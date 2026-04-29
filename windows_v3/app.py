#!/usr/bin/env python3
"""
IQspeakr - Lokale Sprache-zu-Text App für Windows
(Qt-basiert: PySide6 QSystemTrayIcon + QWidget-Overlay.)
"""

import os

# WICHTIG: Vor torch/whisper-Import setzen. In PyInstaller-Bundles führt
# MKL's default multi-threaded Thread-Pool bei wiederholtem transcribe()
# zuverlässig zu Access Violations (0xc0000005 / 0xc0000096). Single-Thread
# kostet bei small-Whisper nur ~0.1-0.3s pro Aufnahme, dafür stabil.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import threading
import subprocess
import tempfile
import json
import re
import sqlite3
import urllib.request
import logging
import sys
import time as _time
from pathlib import Path
from datetime import datetime, date, timedelta

# ffmpeg-PATH (für Whisper-Kompatibilität zu Assets; unsere Transkription
# nutzt numpy-Arrays und braucht ffmpeg selbst nicht).
if getattr(sys, "frozen", False):
    _BUNDLE_BIN = str(Path(sys._MEIPASS) / "bin")
    if _BUNDLE_BIN not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _BUNDLE_BIN + os.pathsep + os.environ.get("PATH", "")

_IQSPEAKR_BIN = str(Path.home() / "IQspeakr" / "bin")
if _IQSPEAKR_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _IQSPEAKR_BIN + os.pathsep + os.environ.get("PATH", "")

logging.basicConfig(
    filename=str(Path.home() / "IQspeakr.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("IQspeakr")
# Im --windowed / frozen-Modus ist sys.stderr None; StreamHandler mit None
# kann sporadische Crashes in Worker-Threads auslösen.
if not getattr(sys, "frozen", False) and sys.stderr is not None:
    log.addHandler(logging.StreamHandler(sys.stderr))

# faulthandler liefert bei C-Level-Crashes (Access Violation) Python-Frame +
# Thread-Dump in ein File - unverzichtbar für Debugging von torch/MKL-Crashes.
import faulthandler
_fh_file = open(str(Path.home() / "IQspeakr.crash.log"), "w", buffering=1)
faulthandler.enable(file=_fh_file, all_threads=True)

# --- Singleton: nur eine Instanz erlauben ---
import msvcrt

_LOCK_FILE = os.path.join(tempfile.gettempdir(), "iqspeakr.lock")
try:
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        _lock_fd.write(" ")
        _lock_fd.flush()
        _lock_fd.seek(0)
        msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.info("IQspeakr läuft bereits - zweite Instanz beendet sich.")
        sys.exit(0)
    _lock_fd.seek(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except SystemExit:
    raise
except Exception as _e:
    log.warning(f"Singleton-Check fehlgeschlagen: {_e}")

# =====================================================================
#  Früher Splash: MUSS vor den schweren Imports (faster-whisper,
#  sounddevice, pynput) gezeigt werden, weil die zusammen 1-3 Sekunden
#  brauchen. Ohne diesen Block sieht der User in der Zeit nichts und
#  denkt, das Doppelklicken hätte nicht funktioniert.
# =====================================================================
from PySide6.QtCore import Qt as _Qt
from PySide6.QtWidgets import (
    QApplication as _QApplication,
    QWidget as _QWidget,
    QLabel as _QLabel,
    QProgressBar as _QProgressBar,
    QVBoxLayout as _QVBoxLayout,
)
from PySide6.QtGui import QGuiApplication as _QGuiApplication


class _StartupSplash(_QWidget):
    def __init__(self):
        super().__init__(
            None,
            _Qt.FramelessWindowHint | _Qt.WindowStaysOnTopHint | _Qt.Tool,
        )
        self.setFixedSize(380, 150)
        layout = _QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(10)

        title = _QLabel("🎤  IQspeakr startet…")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #F1F5F9;")
        title.setAlignment(_Qt.AlignCenter)

        msg = _QLabel(
            "Spracherkennung wird geladen.\n"
            "Einen Moment bitte — das dauert nur kurz."
        )
        msg.setAlignment(_Qt.AlignCenter)
        msg.setStyleSheet("color: #CBD5E1;")

        bar = _QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        # Akzent-Indigo passend zum Theme.
        bar.setStyleSheet(
            "QProgressBar { background: #1F232C; border: 1px solid #2A2D35; "
            "border-radius: 3px; } "
            "QProgressBar::chunk { background: #6366F1; border-radius: 3px; }"
        )

        layout.addWidget(title)
        layout.addWidget(msg)
        layout.addWidget(bar)

        self.setStyleSheet(
            "_StartupSplash { "
            "background: #16181D; "
            "border: 1px solid #2A2D35; "
            "border-radius: 10px; "
            "}"
        )

        try:
            screen = _QGuiApplication.primaryScreen().availableGeometry()
            self.move(
                screen.center().x() - self.width() // 2,
                screen.center().y() - self.height() // 2,
            )
        except Exception:
            pass


_qapp = _QApplication(sys.argv)
_qapp.setQuitOnLastWindowClosed(False)
_splash = _StartupSplash()
_splash.show()
_qapp.processEvents()
log.info("Frueher Splash angezeigt - starte schwere Imports")

# Schwere Imports erst NACH dem Singleton-Check + Splash.
import numpy as np
import sounddevice as sd
# faster-whisper statt openai-whisper: CTranslate2-basiert, kein torch/MKL.
# Umgeht den reproduzierbaren PyTorch-Access-Violation-Bug beim 2. Inferenz-
# Call in PyInstaller-Bundles (PyTorch-Issue #131662).
from faster_whisper import WhisperModel
import pyperclip
from pynput import keyboard
from pynput.keyboard import Key, KeyCode, Controller

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QByteArray, QSize
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget,
    QMessageBox, QInputDialog, QMainWindow,
    QDialog, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout, QDialogButtonBox,
    QListWidget, QListWidgetItem, QStackedWidget, QPushButton,
    QCheckBox, QLineEdit, QTextEdit, QPlainTextEdit, QProgressBar, QComboBox,
    QFrame, QFormLayout, QSizePolicy, QScrollArea, QGroupBox,
)
from PySide6.QtGui import (
    QIcon, QPixmap, QAction, QActionGroup, QPainter, QColor,
    QGuiApplication, QFont, QCursor,
)
from PySide6.QtSvg import QSvgRenderer

# --- Pfade ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_CONFIG = os.path.join(APP_DIR, "config.json")
USER_DIR = str(Path.home() / "IQspeakr")
CONFIG_PATH = os.path.join(USER_DIR, "config.json")

# Hauptfenster-State (History etc.) liegt in %APPDATA%\IQspeakr\, getrennt
# von der Legacy-Config in ~/IQspeakr\, damit bestehende User ihre
# Einstellungen behalten.
APPDATA_DIR = os.path.join(
    os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")),
    "IQspeakr",
)
HISTORY_PATH = os.path.join(APPDATA_DIR, "history.json")
HISTORY_MAX = 10
# SQLite für das Dashboard - append-only mit potenziell vielen tausend
# Einträgen, Time-Range-Queries für Heatmap. Liegt in %APPDATA%\IQspeakr.
STATS_DB_PATH = os.path.join(APPDATA_DIR, "stats.db")
# Wörterbuch: korrekte Schreibweisen für Eigennamen, die Whisper falsch
# versteht (z.B. "IQspeakr" -> "Ich Sprecher"). Wird vor Ollama-Cleanup
# angewendet.
DICTIONARY_PATH = os.path.join(APPDATA_DIR, "dictionary.json")

os.makedirs(USER_DIR, exist_ok=True)
os.makedirs(APPDATA_DIR, exist_ok=True)
if not os.path.exists(CONFIG_PATH) and os.path.exists(BUNDLE_CONFIG):
    import shutil
    shutil.copy2(BUNDLE_CONFIG, CONFIG_PATH)

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_BASE = "http://localhost:11434"
OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
# Inno-Setup-basiert, per-user-Install (kein Admin), Pfad fix laut ollama.iss.
_LOCAL_APPDATA = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
OLLAMA_INSTALL_DIR = os.path.join(_LOCAL_APPDATA, "Programs", "Ollama")
OLLAMA_UNINSTALLER = os.path.join(OLLAMA_INSTALL_DIR, "unins000.exe")
OLLAMA_EXE = os.path.join(OLLAMA_INSTALL_DIR, "ollama.exe")
SAMPLE_RATE = 16000

# Asset-Pfad für die App-Icon-Datei (in der Sidebar genutzt).
APP_ICON_PATH = os.path.join(APP_DIR, "icon.ico")

# =====================================================================
#  Theme-Tokens. Eine Stelle für alle Farben - sonst driftet das auseinander.
# =====================================================================

THEME_BG            = "#16181D"
THEME_BG_SIDEBAR    = "#0F1115"
THEME_BG_CARD       = "#1B1E25"
THEME_BG_INPUT      = "#1F232C"
THEME_BG_HOVER      = "rgba(255, 255, 255, 0.05)"
THEME_BORDER        = "#2A2D35"
THEME_BORDER_HOVER  = "#3A3F4A"
THEME_BORDER_SOFT   = "rgba(255, 255, 255, 0.06)"
THEME_TEXT          = "#F1F5F9"   # heller, kontrastreicher Body-Text
THEME_TEXT_SECONDARY = "#CBD5E1"  # Form-Labels, Sub-Texte
THEME_TEXT_MUTED    = "#8C92A0"   # Meta / Timestamps
THEME_ACCENT        = "#6366F1"   # Indigo
THEME_ACCENT_HOVER  = "#7B7DF5"
THEME_ACCENT_SOFT   = "rgba(99, 102, 241, 0.18)"
THEME_DANGER        = "#EF4444"
THEME_SUCCESS       = "#22C55E"
THEME_WARNING       = "#F59E0B"


def apply_app_theme(qapp):
    """Setzt System-Font + globale QSS auf die QApplication. Wird einmal
    in main() aufgerufen, danach erbt jedes Widget davon. Lokale
    setStyleSheet()-Aufrufe in einzelnen Klassen ergänzen / spezialisieren."""
    # Segoe UI Variable ist Windows-11-Standard, mit Fallback auf
    # Segoe UI (Win10) und Inter falls jemand das eingebunden hat.
    font = QFont("Segoe UI Variable Display")
    if not font.exactMatch():
        font = QFont("Segoe UI Variable")
    if not font.exactMatch():
        font = QFont("Segoe UI")
    font.setPointSizeF(10.0)
    # Medium-Weight = klarer lesbar als Default-Regular bei kleinem
    # Punkt-Wert auf Win11.
    font.setWeight(QFont.Medium)
    font.setHintingPreference(QFont.PreferFullHinting)
    qapp.setFont(font)

    qss = f"""
    /* ---- Generic ---- */
    QWidget {{
        background: transparent;
        color: {THEME_TEXT};
    }}
    QMainWindow, QDialog {{
        background: {THEME_BG};
    }}
    QToolTip {{
        background: {THEME_BG_CARD};
        color: {THEME_TEXT};
        border: 1px solid {THEME_BORDER};
        padding: 4px 6px;
    }}

    /* ---- Labels / Section-Header ---- */
    QLabel {{
        color: {THEME_TEXT};
        background: transparent;
    }}
    QLabel[role="muted"] {{
        color: {THEME_TEXT_MUTED};
    }}
    QLabel[role="h1"] {{
        color: {THEME_TEXT};
        font-size: 22px;
        font-weight: 700;
    }}
    QLabel[role="sub"] {{
        color: {THEME_TEXT_SECONDARY};
        font-size: 13px;
    }}

    /* ---- Cards / GroupBox ---- */
    QGroupBox {{
        background: {THEME_BG_CARD};
        border: 1px solid {THEME_BORDER};
        border-radius: 12px;
        margin-top: 24px;
        padding: 36px 24px 24px 24px;
        font-weight: 600;
        font-size: 13px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 20px;
        padding: 0 8px;
        color: {THEME_TEXT};
        background: {THEME_BG};
    }}

    /* ---- Inputs ---- */
    QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
        background: {THEME_BG_INPUT};
        color: {THEME_TEXT};
        border: 1px solid {THEME_BORDER};
        border-radius: 8px;
        padding: 7px 10px;
        selection-background-color: {THEME_ACCENT};
        selection-color: white;
    }}
    QPlainTextEdit, QTextEdit {{
        padding: 9px 10px;
    }}
    QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QComboBox:hover {{
        border-color: {THEME_BORDER_HOVER};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{
        border-color: {THEME_ACCENT};
        outline: 0;
    }}
    QLineEdit:disabled, QComboBox:disabled, QPlainTextEdit:disabled {{
        color: {THEME_TEXT_MUTED};
        background: #181B22;
    }}

    /* ---- ComboBox dropdown ---- */
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {THEME_TEXT_SECONDARY};
        margin-right: 8px;
        width: 0;
        height: 0;
    }}
    QComboBox QAbstractItemView {{
        background: {THEME_BG_CARD};
        color: {THEME_TEXT};
        border: 1px solid {THEME_BORDER};
        border-radius: 8px;
        padding: 4px;
        selection-background-color: {THEME_ACCENT};
        selection-color: white;
        outline: 0;
    }}
    QComboBox QAbstractItemView::item {{
        padding: 6px 10px;
        border-radius: 6px;
        min-height: 22px;
    }}

    /* ---- Buttons ---- */
    QPushButton {{
        background: {THEME_BG_INPUT};
        color: {THEME_TEXT};
        border: 1px solid {THEME_BORDER};
        border-radius: 8px;
        padding: 8px 16px;
        min-height: 18px;
        font-weight: 500;
    }}
    QPushButton:hover {{
        background: {THEME_BG_HOVER};
        border-color: {THEME_BORDER_HOVER};
    }}
    QPushButton:pressed {{
        background: #15171C;
    }}
    QPushButton:disabled {{
        color: {THEME_TEXT_MUTED};
        background: #181B22;
        border-color: {THEME_BORDER};
    }}
    QPushButton[role="primary"] {{
        background: {THEME_ACCENT};
        color: white;
        border: 1px solid {THEME_ACCENT};
    }}
    QPushButton[role="primary"]:hover {{
        background: {THEME_ACCENT_HOVER};
        border-color: {THEME_ACCENT_HOVER};
    }}
    QPushButton[role="primary"]:disabled {{
        background: #2A2A50;
        color: #8E8FBF;
        border-color: #2A2A50;
    }}
    QPushButton[role="danger"] {{
        background: transparent;
        color: {THEME_DANGER};
        border: 1px solid {THEME_DANGER};
    }}
    QPushButton[role="danger"]:hover {{
        background: rgba(239, 68, 68, 0.10);
    }}

    /* ---- CheckBox ---- */
    QCheckBox {{
        color: {THEME_TEXT};
        spacing: 8px;
        padding: 2px 0;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {THEME_BORDER_HOVER};
        border-radius: 4px;
        background: {THEME_BG_INPUT};
    }}
    QCheckBox::indicator:hover {{
        border-color: {THEME_ACCENT};
    }}
    QCheckBox::indicator:checked {{
        background: {THEME_ACCENT};
        border-color: {THEME_ACCENT};
        image: none;
    }}

    /* ---- ProgressBar ---- */
    QProgressBar {{
        background: {THEME_BG_INPUT};
        border: 1px solid {THEME_BORDER};
        border-radius: 6px;
        text-align: center;
        color: {THEME_TEXT};
        height: 14px;
    }}
    QProgressBar::chunk {{
        background: {THEME_ACCENT};
        border-radius: 5px;
    }}

    /* ---- Scrollbars ---- */
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: #2F3340;
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: #3A3F4D;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        background: transparent;
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
    }}
    QScrollBar::handle:horizontal {{
        background: #2F3340;
        border-radius: 5px;
        min-width: 24px;
    }}

    /* ---- Lists ---- */
    QListWidget {{
        background: {THEME_BG_CARD};
        color: {THEME_TEXT};
        border: 1px solid {THEME_BORDER};
        border-radius: 10px;
        padding: 6px;
        outline: 0;
    }}
    QListWidget::item {{
        padding: 10px 12px;
        border-radius: 6px;
    }}
    QListWidget::item:hover {{
        background: {THEME_BG_HOVER};
    }}
    QListWidget::item:selected {{
        background: {THEME_ACCENT_SOFT};
        color: {THEME_TEXT};
    }}
    """
    qapp.setStyleSheet(qss)


# =====================================================================
#  Lucide-Icons inline (lucide.dev, ISC). Nur die drei Pfade die wir
#  brauchen - kein Asset-File nötig, kein extra Package.
# =====================================================================

_LUCIDE_HOME = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>"""

_LUCIDE_TYPE = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 7 4 4 20 4 20 7"/><line x1="9" x2="15" y1="20" y2="20"/><line x1="12" x2="12" y1="4" y2="20"/></svg>"""

_LUCIDE_SETTINGS = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>"""

_LUCIDE_COPY = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>"""

_LUCIDE_CHECK = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>"""

_LUCIDE_BAR_CHART = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>"""

_LUCIDE_INFO = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="16" y2="12"/><line x1="12" x2="12.01" y1="8" y2="8"/></svg>"""

_LUCIDE_BOOK = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>"""
_LUCIDE_PLUS = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>"""
_LUCIDE_PENCIL = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/></svg>"""
_LUCIDE_TRASH = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>"""


def _lucide_icon(svg_template, size=18, color=None):
    """Rendert einen Lucide-SVG-Template-String zur QIcon. `color` ersetzt
    den `currentColor`-Stroke. Liefert ein QIcon mit der angegebenen Pixel-
    Größe (HiDPI-aware via QPixmap.devicePixelRatio nicht nötig - bei
    den Sidebar-Sizes fällt sub-pixel-Aliasing nicht ins Auge)."""
    color = color or THEME_TEXT_SECONDARY
    svg = svg_template.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


# =====================================================================
#  Qt-basiertes Pill-Overlay (stabil, thread-safe via Signals).
# =====================================================================

class PillOverlay(QWidget):
    """Pill-Overlay mit 7 Live-Waveform-Balken unten-mittig.
    Nur sichtbar während Aufnahme - sonst komplett versteckt.
    Audio-Thread schreibt nur atomische Python-Attribute (thread-safe in
    CPython), der QTimer im Main-Thread liest sie - keine Cross-Thread
    Qt-Signals aus C-Callbacks nötig (vermeidet Stack-Races unter
    Whisper/Torch-Parallelbetrieb)."""

    BAR_COUNT = 7
    W = 180
    H = 36
    ACTIVE_ALPHA = 0.92
    MARGIN_BOTTOM = 60  # Abstand zur Windows-Taskleiste

    def __init__(self, enabled=True):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.Tool | Qt.WindowDoesNotAcceptFocus,
        )
        self.enabled = enabled
        self._levels = [0.0] * self.BAR_COUNT
        self._active = False
        self._current_alpha = 0.0
        self._target_alpha = self.ACTIVE_ALPHA

        if not enabled:
            log.info("PillOverlay: deaktiviert (config.overlay_enabled=false)")
            return

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.resize(self.W, self.H)
        self._move_to_primary_screen()

        self._timer = QTimer(self)
        self._timer.setInterval(40)  # ~25 fps
        self._timer.timeout.connect(self._tick)
        # Timer läuft nur während Aufnahme - im Idle nichts zu animieren.

        # Initial unsichtbar. Overlay erscheint erst bei set_recording(True)
        # und verschwindet wieder bei set_recording(False).
        self.setWindowOpacity(0.0)

    def _move_to_primary_screen(self):
        try:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            x = screen.x() + (screen.width() - self.W) // 2
            y = screen.y() + screen.height() - self.H - self.MARGIN_BOTTOM
            self.move(x, y)
        except Exception as e:
            log.warning(f"PillOverlay: primaryScreen() Fehler: {e}")

    def _tick(self):
        # Läuft nur während Aufnahme. Opacity auf Active-Level faden,
        # Balken decay.
        diff = self._target_alpha - self._current_alpha
        if abs(diff) > 0.005:
            self._current_alpha += diff * 0.2
        self.setWindowOpacity(self._current_alpha)
        self._levels = [l * 0.90 for l in self._levels]
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Pille (leicht transparenter dunkler Hintergrund)
        p.setBrush(QColor(30, 30, 30, 235))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, self.W, self.H, self.H // 2, self.H // 2)

        # 7 weiße Balken in der Mitte
        bar_w = 3
        bar_gap = 4
        total = self.BAR_COUNT * bar_w + (self.BAR_COUNT - 1) * bar_gap
        start_x = (self.W - total) // 2
        cy = self.H // 2
        max_bar_h = self.H - 12

        p.setBrush(QColor(255, 255, 255))
        for i in range(self.BAR_COUNT):
            x1 = start_x + i * (bar_w + bar_gap)
            bar_h = max(2, int(self._levels[i] * max_bar_h))
            y1 = cy - bar_h // 2
            p.drawRoundedRect(x1, y1, bar_w, bar_h, 1.5, 1.5)
        p.end()

    # API für Audio-Thread: nur atomische Attribut-Zuweisung
    # (in CPython thread-safe). Das Tick im Main-Thread liest.
    def set_levels(self, levels):
        if self.enabled:
            self._levels = list(levels)

    def set_recording(self, on):
        """Main-Thread-only. Overlay nur während Aufnahme sichtbar,
        sonst versteckt."""
        if not self.enabled:
            return
        self._active = bool(on)
        if on:
            self._move_to_primary_screen()
            self._current_alpha = 0.0
            self._target_alpha = self.ACTIVE_ALPHA
            self.setWindowOpacity(0.0)
            self.show()
            self._timer.start()
        else:
            self._levels = [0.0] * self.BAR_COUNT
            self._timer.stop()
            self.setWindowOpacity(0.0)
            self.hide()


CLEANUP_PROMPT_LOCKER = """Bereinige den gesprochenen Text minimal-invasiv:
- Füllwörter weg (ähm, äh, also, halt, quasi, irgendwie)
- Wortdoppelungen / Stotterer weg
- Satzzeichen + Großschreibung korrigieren
- Offensichtliche Grammatikfehler korrigieren
NICHT umformulieren, NICHT zusammenfassen, Stil bewahren.

Antworte NUR mit dem bereinigten Text.

Text: {text}"""

CLEANUP_PROMPT_FORMAL = """Bereinige den gesprochenen Text und hebe ihn in förmliches Schriftdeutsch:
- Füllwörter, Wortdoppelungen, Stotterer weg
- Satzzeichen, Großschreibung, Grammatik korrigieren
- Umgangssprache schriftsprachlich ersetzen ("kriegen" -> "erhalten", "ne" -> "eine")
- Verkürzungen ausschreiben ("geht's" -> "geht es")
- Sätze umstellen, wenn der Schriftstil das verlangt
NICHT inhaltlich ändern, NICHTS hinzufügen.

Antworte NUR mit dem bereinigten Text.

Text: {text}"""

CLEANUP_PROMPT_SEHR_LOCKER = """Entferne nur Fülllaute aus dem Text. Sonst NICHTS ändern.
- "ähm", "äh", "öh", "mhm" entfernen
- Direkte Wortdoppelungen wie "ich ich" entfernen (NICHT "sehr sehr")
- Punkt am Satzende setzen wenn fehlt

NICHT Grammatik ändern, NICHT umstellen, NICHT Füllwörter wie "halt"/"also" entfernen.
Antworte NUR mit dem Text minus Fülllaute.

Text: {text}"""

# Backward-compatible alias - der alte Name wird ggf. noch referenziert.
CLEANUP_PROMPT = CLEANUP_PROMPT_LOCKER

# Default-Beispiel im "Eigene Anweisungen"-Feld der Individuell-Karte.
CUSTOM_PROMPT_EXAMPLE = """Achte besonders auf Fachbegriffe aus der IT (z.B. "API", "Repository", "Pull Request"). Schreibe diese englischen Begriffe NICHT klein, auch wenn sie im Satzinneren stehen. Behalte du/Sie-Anrede so wie diktiert. Wenn ich eine Aufzählung mache, formatiere sie als Bulletpoints."""

# Reihenfolge + Labels für die Individuell-Checkboxen. Key matched config.
CUSTOM_OPTIONS = [
    ("filler",     "Füllwörter entfernen (ähm, äh, halt, quasi)"),
    ("repeats",    "Wortdoppelungen / Stotterer entfernen"),
    ("punct",      "Satzzeichen und Großschreibung korrigieren"),
    ("grammar",    "Offensichtliche Grammatikfehler korrigieren"),
    ("reorder",    "Satzumstellungen erlauben (wenn nötig)"),
    ("formalize",  "Umgangssprache in Schriftsprache überführen"),
]


def build_custom_prompt(checkboxes, extra_prompt):
    """Generiert aus den Checkbox-Werten + freier Zusatzanweisung den
    Custom-Cleanup-Prompt. checkboxes ist ein dict {key: bool}."""
    rules_allowed = []
    rules_forbidden = []
    if checkboxes.get("filler"):
        rules_allowed.append("- Füllwörter entfernen (ähm, äh, also, sozusagen, halt, quasi, irgendwie, eben, ja, nun)")
    else:
        rules_forbidden.append("- Füllwörter entfernen (sie sind Stil)")
    if checkboxes.get("repeats"):
        rules_allowed.append("- Wortdoppelungen und Stotterer entfernen")
    if checkboxes.get("punct"):
        rules_allowed.append("- Satzzeichen und Großschreibung korrigieren")
    if checkboxes.get("grammar"):
        rules_allowed.append("- Offensichtliche Grammatikfehler korrigieren")
    if checkboxes.get("reorder"):
        rules_allowed.append("- Sätze umstellen, wenn grammatikalisch oder stilistisch nötig")
    else:
        rules_forbidden.append("- Sätze umstellen oder umformulieren")
    if checkboxes.get("formalize"):
        rules_allowed.append("- Umgangssprache durch schriftsprachliche Äquivalente ersetzen")
    else:
        rules_forbidden.append("- Stil verändern (umgangssprachlich -> schriftsprachlich)")
    # Immer verboten:
    rules_forbidden.append("- Inhalt streichen oder zusammenfassen")
    rules_forbidden.append("- Eigene Aussagen hinzufügen")

    parts = ["Du bereinigst gesprochene Sprache nach den folgenden Regeln. Inhaltlich nichts hinzufügen oder entfernen.\n"]
    if rules_allowed:
        parts.append("ERLAUBT:")
        parts.extend(rules_allowed)
        parts.append("")
    if rules_forbidden:
        parts.append("VERBOTEN:")
        parts.extend(rules_forbidden)
        parts.append("")
    extra = (extra_prompt or "").strip()
    if extra:
        parts.append("ZUSAETZLICHE ANWEISUNGEN:")
        parts.append(extra)
        parts.append("")
    parts.append("Antworte NUR mit dem bereinigten Text, ohne Erklärungen, ohne Anführungszeichen.")
    parts.append("")
    parts.append("Text: {text}")
    return "\n".join(parts)


def get_cleanup_prompt(config):
    """Liefert den aktiven Cleanup-Prompt-Template-String basierend auf
    config["style"]. Default: 'locker' (Verhalten vor V2)."""
    style = (config or {}).get("style", "locker")
    if style == "formal":
        return CLEANUP_PROMPT_FORMAL
    if style == "sehr_locker":
        return CLEANUP_PROMPT_SEHR_LOCKER
    if style == "custom":
        cust = (config or {}).get("style_custom", {}) or {}
        return build_custom_prompt(
            cust.get("checkboxes", {}) or {},
            cust.get("extra_prompt", "") or "",
        )
    return CLEANUP_PROMPT_LOCKER

# Beispieltext der in der Style-View den Vorher/Nachher-Effekt zeigt.
STYLE_SAMPLE_INPUT = "ähm also ich wollte nur kurz sagen dass das ja eigentlich ganz gut funktioniert halt"
STYLE_SAMPLE_OUTPUTS = {
    "formal":      "Ich möchte kurz mitteilen, dass dies grundsätzlich gut funktioniert.",
    "locker":      "Ich wollte nur kurz sagen, dass das eigentlich ganz gut funktioniert.",
    "sehr_locker": "Also ich wollte nur kurz sagen, dass das ja eigentlich ganz gut funktioniert halt.",
    "custom":      "(Eigene Anweisungen + Checkboxen bestimmen das Ergebnis.)",
}


# --- Hotkey ---
_MODIFIER_ALIASES = {
    "ctrl":    {Key.ctrl, Key.ctrl_l, Key.ctrl_r},
    "control": {Key.ctrl, Key.ctrl_l, Key.ctrl_r},
    "shift":   {Key.shift, Key.shift_l, Key.shift_r},
    "alt":     {Key.alt, Key.alt_l, Key.alt_r},
    "option":  {Key.alt, Key.alt_l, Key.alt_r},
    "cmd":     {Key.cmd, Key.cmd_l, Key.cmd_r},
    "command": {Key.cmd, Key.cmd_l, Key.cmd_r},
    "win":     {Key.cmd, Key.cmd_l, Key.cmd_r},
}

_NAMED_KEYS = {
    "space": Key.space,
    "enter": Key.enter,
    "tab":   Key.tab,
    "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
    "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
    "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
}

HOTKEY_DISPLAY = {
    "ctrl": "Strg", "control": "Strg",
    "shift": "Umschalt",
    "alt": "Alt", "option": "Alt",
    "cmd": "Win", "command": "Win", "win": "Win",
    "space": "Leertaste",
    "enter": "Eingabe",
    "tab": "Tab",
}

# Qt-Key -> interner Hotkey-String (für Custom-Hotkey-Recorder)
_QT_MOD_TO_NAME = {
    Qt.Key_Control: "ctrl",
    Qt.Key_Shift:   "shift",
    Qt.Key_Alt:     "alt",
    Qt.Key_Meta:    "win",
}
_QT_NAMED_KEYS = {
    Qt.Key_Space:  "space",
    Qt.Key_Return: "enter",
    Qt.Key_Enter:  "enter",
    Qt.Key_Tab:    "tab",
    Qt.Key_F1: "f1", Qt.Key_F2: "f2", Qt.Key_F3: "f3", Qt.Key_F4: "f4",
    Qt.Key_F5: "f5", Qt.Key_F6: "f6", Qt.Key_F7: "f7", Qt.Key_F8: "f8",
    Qt.Key_F9: "f9", Qt.Key_F10: "f10", Qt.Key_F11: "f11", Qt.Key_F12: "f12",
}


def _is_modifier_name(name):
    return name in _MODIFIER_ALIASES


def parse_hotkey(hotkey_str):
    matchers = []
    for part in hotkey_str.lower().split("+"):
        part = part.strip()
        if not part:
            continue
        if part in _MODIFIER_ALIASES:
            matchers.append(_MODIFIER_ALIASES[part])
        elif part in _NAMED_KEYS:
            matchers.append(_NAMED_KEYS[part])
        elif len(part) == 1:
            matchers.append(KeyCode.from_char(part))
    return matchers


def hotkey_display(hotkey_str):
    parts = hotkey_str.lower().split("+")
    display_parts = []
    for part in parts:
        part = part.strip()
        if part in HOTKEY_DISPLAY:
            display_parts.append(HOTKEY_DISPLAY[part])
        else:
            display_parts.append(part.upper())
    return "+".join(display_parts)


def _hotkey_is_single_modifier(hotkey_str):
    parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
    return len(parts) == 1 and _is_modifier_name(parts[0])


def _hotkey_is_all_modifiers(hotkey_str):
    """True wenn die Kombi ausschließlich aus Modifiern besteht (z.B. 'ctrl',
    'ctrl+shift'). Nur dann sind Hold/Tap/Double-Tap sinnvoll - bei Kombis mit
    Buchstaben oder F-Tasten gibt's nämlich keine klare 'lang gehalten'-Semantik."""
    parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
    if not parts:
        return False
    return all(_is_modifier_name(p) for p in parts)


def _whisper_model_cached(size):
    # faster-whisper lädt Modelle aus Huggingface-Cache (andere Struktur
    # als openai-whisper). Konservativ: True zurückgeben, dann überspringt
    # die UI den "herunterladen?"-Dialog. Download passiert transparent beim
    # ersten load.
    return True


# --- Config ---
DEFAULT_CONFIG = {
    "hotkey": "ctrl+shift",
    # tiny ist ~3x schneller als base bei Diktat-Qualität im Alltag
    # praktisch identisch. User kann in Settings auf base/small/medium hoch.
    "whisper_model": "tiny",
    # 1B-Param-Variante läuft auf CPU ~3x schneller als 3B für reines
    # Cleanup. Älteren Configs bleibt ihre alte Wahl - dies ist nur
    # die Erst-Install-Default.
    "ollama_model": "llama3.2:1b",
    # User-Toggle "Ollama-Backend aktiv". False -> "ollama serve" wird
    # gekillt, _refresh_worker startet nicht neu, Cleanup wird übersprungen.
    "ollama_active": True,
    # Default AUS: Whisper macht bereits Punktuation, Großschreibung und
    # filtert Füllwörter. Cleanup kostet 3-7s pro Aufnahme. Power-User
    # können es bei Bedarf in Settings einschalten.
    "cleanup_enabled": False,
    "language": "de",
    "overlay_enabled": True,
    # Status-Notifications via Tray-Bubble. False = nur Fehler werden gezeigt
    # ("Modell-Lade gescheitert", Whisper-Crash). Info wie "Kein Text erkannt"
    # oder "Modell geladen" wird unterdrückt.
    "notify_enabled": True,
    # Style-Auswahl für die Cleanup-Prompt: formal | locker | sehr_locker | custom
    "style": "locker",
    # Wird nur ausgewertet wenn style == "custom".
    "style_custom": {
        "checkboxes": {
            "filler": True,
            "repeats": True,
            "punct": True,
            "grammar": True,
            "reorder": False,
            "formalize": False,
        },
        "extra_prompt": "",
    },
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
            config = DEFAULT_CONFIG.copy()
            config.update(saved)
            return config
    return DEFAULT_CONFIG.copy()


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# =====================================================================
#  History-Store: persistiert die letzten HISTORY_MAX Transkripte.
# =====================================================================

class HistoryStore(QObject):
    """Einfacher JSON-FIFO-Store für die letzten HISTORY_MAX Transkripte.
    Schreibt %APPDATA%\\IQspeakr\\history.json. Emit changed() nach add()
    damit angeschlossene Views (HomeView) sich aktualisieren - über Qt-
    Signal, da add() aus dem Audio/Whisper-Thread aufgerufen wird."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = self._load()

    def _load(self):
        try:
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return [x for x in data if isinstance(x, dict) and "text" in x]
        except Exception as e:
            log.warning(f"HistoryStore: Laden fehlgeschlagen: {e}")
        return []

    def _save(self):
        try:
            os.makedirs(APPDATA_DIR, exist_ok=True)
            with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(self._items, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"HistoryStore: Speichern fehlgeschlagen: {e}")

    def items(self):
        # Defensive Kopie, damit Aufrufer die Liste nicht aus Versehen
        # mutieren während der Audio-Thread schreibt.
        return list(self._items)

    def add(self, text):
        text = (text or "").strip()
        if not text:
            return
        entry = {
            "ts": int(_time.time()),
            "text": text,
        }
        # Neueste oben. FIFO-Trim auf HISTORY_MAX.
        self._items.insert(0, entry)
        if len(self._items) > HISTORY_MAX:
            self._items = self._items[:HISTORY_MAX]
        self._save()
        self.changed.emit()

    def clear(self):
        self._items = []
        self._save()
        self.changed.emit()


# =====================================================================
#  Stats-Store: SQLite-basierter, append-only Speicher für das Dashboard.
#  - Pro Aufnahme ein Row (timestamp, word_count, duration_sec).
#  - Liegt unabhängig von der 10er-History in stats.db.
#  - duration_sec=0 bedeutet "Dauer unbekannt" (z.B. migrierte
#    History-Einträge) und wird in WPM-Berechnung ausgeschlossen.
# =====================================================================

class StatsStore(QObject):

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        os.makedirs(APPDATA_DIR, exist_ok=True)
        # check_same_thread=False, weil record() aus dem Audio-/Whisper-
        # Worker-Thread aufgerufen wird. Wir serialisieren manuell über
        # _lock - das reicht für unseren append-dominanten Workload.
        self._conn = sqlite3.connect(STATS_DB_PATH, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS stats (
                    ts INTEGER NOT NULL,
                    word_count INTEGER NOT NULL,
                    duration_sec REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_stats_ts ON stats(ts);
                """
            )
            self._conn.commit()

    # --- Schreiben ---
    def record(self, ts, word_count, duration_sec):
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO stats (ts, word_count, duration_sec) VALUES (?, ?, ?)",
                    (int(ts), int(max(0, word_count)), float(max(0.0, duration_sec))),
                )
                self._conn.commit()
        except Exception as e:
            log.warning(f"StatsStore.record fehlgeschlagen: {e}")
            return
        self.changed.emit()

    def is_empty(self):
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM stats").fetchone()
        return (row["c"] if row else 0) == 0

    # --- Aggregationen ---
    def total_words(self):
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(word_count), 0) AS s FROM stats").fetchone()
        return int(row["s"] if row else 0)

    def avg_wpm(self):
        """Wörter pro Minute über alle Einträge mit duration_sec > 0.
        Liefert None wenn keine Einträge mit Dauer existieren."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(word_count), 0) AS w, COALESCE(SUM(duration_sec), 0) AS d "
                "FROM stats WHERE duration_sec > 0"
            ).fetchone()
        words = int(row["w"]) if row else 0
        secs = float(row["d"]) if row else 0.0
        if secs <= 0:
            return None
        return words / (secs / 60.0)

    def daily_counts(self, days_back):
        """Liefert dict[date] -> int für die letzten `days_back` Tage,
        inkl. heute. Tage ohne Eintrag fehlen im Dict."""
        cutoff = int((datetime.now() - timedelta(days=days_back - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp())
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM stats WHERE ts >= ? ORDER BY ts ASC",
                (cutoff,),
            ).fetchall()
        out = {}
        for r in rows:
            d = date.fromtimestamp(int(r["ts"]))
            out[d] = out.get(d, 0) + 1
        return out

    def all_active_days(self):
        """Set aller Tage mit mindestens einem Eintrag, über die ganze DB."""
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT ts FROM stats").fetchall()
        return {date.fromtimestamp(int(r["ts"])) for r in rows}

    def current_streak(self):
        days = self.all_active_days()
        if not days:
            return 0
        today = date.today()
        # Streak gilt auch wenn heute noch nichts da ist - dann beginnt
        # er bei "gestern". Andernfalls macht ein Morgen-Aufruf den
        # Wert gerade kaputt.
        cur = today if today in days else today - timedelta(days=1)
        if cur not in days:
            return 0
        streak = 0
        while cur in days:
            streak += 1
            cur -= timedelta(days=1)
        return streak

    def longest_streak(self):
        days = sorted(self.all_active_days())
        if not days:
            return 0
        longest = 1
        cur = 1
        for i in range(1, len(days)):
            if days[i] - days[i - 1] == timedelta(days=1):
                cur += 1
                longest = max(longest, cur)
            else:
                cur = 1
        return longest

    def import_legacy_history(self, history_items):
        """Einmalig beim ersten Start: alte history.json-Einträge in die
        DB einspielen, damit das Dashboard sofort etwas zu zeigen hat.
        Macht nichts wenn die DB schon Einträge hat."""
        if not self.is_empty():
            return 0
        n = 0
        with self._lock:
            for item in history_items or []:
                ts = int(item.get("ts") or 0)
                text = (item.get("text") or "").strip()
                if not ts or not text:
                    continue
                wc = len(text.split())
                self._conn.execute(
                    "INSERT INTO stats (ts, word_count, duration_sec) VALUES (?, ?, 0)",
                    (ts, wc),
                )
                n += 1
            self._conn.commit()
        if n:
            self.changed.emit()
            log.info(f"StatsStore: {n} Legacy-History-Einträge migriert")
        return n


# =====================================================================
#  Wörterbuch: Eigennamen + falsche Whisper-Schreibungen. apply() ersetzt
#  alle Varianten case-insensitive durch die korrekte Schreibweise.
#  correct_names() liefert die Liste für den Cleanup-Prompt ("nicht
#  kaputt machen"). Wird vor Ollama-Cleanup im Audio-Thread angewendet.
# =====================================================================

class DictionaryStore(QObject):
    """Glossar von Eigennamen + falschen Whisper-Schreibungen. apply()
    ersetzt im Whisper-Output alle Varianten case-insensitive durch die
    korrekte Schreibweise. correct_names() liefert die Liste der Eigen-
    namen, die der Cleanup-Prompt an Ollama mitschickt ("nicht kaputt
    machen")."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = self._load()

    def _load(self):
        try:
            if os.path.exists(DICTIONARY_PATH):
                with open(DICTIONARY_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        cleaned = []
                        for it in data:
                            if not isinstance(it, dict):
                                continue
                            correct = (it.get("correct") or "").strip()
                            variants = it.get("variants") or []
                            if not correct or not isinstance(variants, list):
                                continue
                            v_clean = [v.strip() for v in variants
                                       if isinstance(v, str) and v.strip()]
                            if not v_clean:
                                continue
                            cleaned.append({"correct": correct,
                                            "variants": v_clean})
                        return cleaned
        except Exception as e:
            log.warning(f"DictionaryStore: Laden fehlgeschlagen: {e}")
        return []

    def _save(self):
        try:
            os.makedirs(APPDATA_DIR, exist_ok=True)
            with open(DICTIONARY_PATH, "w", encoding="utf-8") as f:
                json.dump(self._items, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"DictionaryStore: Speichern fehlgeschlagen: {e}")

    def entries(self):
        # Defensive Tiefkopie - Aufrufer sollen die interne Liste nicht
        # versehentlich mutieren.
        return [{"correct": e["correct"], "variants": list(e["variants"])}
                for e in self._items]

    def correct_names(self):
        return [e["correct"] for e in self._items if e.get("correct")]

    def find_by_correct(self, correct):
        """Index des Eintrags mit gleicher korrekter Schreibweise (case-
        insensitive), oder -1."""
        target = (correct or "").strip().lower()
        if not target:
            return -1
        for i, e in enumerate(self._items):
            if e["correct"].lower() == target:
                return i
        return -1

    def add(self, correct, variants):
        """Neuen Eintrag anlegen. Ruft KEINE Duplicate-Check; UI muss vorher
        find_by_correct nutzen und ggf. merge_variants aufrufen."""
        correct = (correct or "").strip()
        v_clean = [v.strip() for v in (variants or []) if v and v.strip()]
        if not correct or not v_clean:
            return False
        # Dedupe Varianten innerhalb des Eintrags case-insensitive.
        seen = set()
        deduped = []
        for v in v_clean:
            if v.lower() in seen:
                continue
            seen.add(v.lower())
            deduped.append(v)
        self._items.append({"correct": correct, "variants": deduped})
        self._save()
        self.changed.emit()
        return True

    def update(self, idx, correct, variants):
        if not (0 <= idx < len(self._items)):
            return False
        correct = (correct or "").strip()
        v_clean = [v.strip() for v in (variants or []) if v and v.strip()]
        if not correct or not v_clean:
            return False
        seen = set()
        deduped = []
        for v in v_clean:
            if v.lower() in seen:
                continue
            seen.add(v.lower())
            deduped.append(v)
        self._items[idx] = {"correct": correct, "variants": deduped}
        self._save()
        self.changed.emit()
        return True

    def remove(self, idx):
        if not (0 <= idx < len(self._items)):
            return False
        del self._items[idx]
        self._save()
        self.changed.emit()
        return True

    def merge_variants(self, idx, new_variants):
        """Hängt neue Varianten an einen bestehenden Eintrag an (für den
        'Eintrag existiert bereits'-Flow)."""
        if not (0 <= idx < len(self._items)):
            return False
        existing = self._items[idx]
        seen = {v.lower() for v in existing["variants"]}
        added = []
        for v in (new_variants or []):
            v = (v or "").strip()
            if not v or v.lower() in seen:
                continue
            seen.add(v.lower())
            added.append(v)
        if not added:
            return False
        existing["variants"].extend(added)
        self._save()
        self.changed.emit()
        return True

    def apply(self, text):
        """Ersetzt alle Varianten im Text durch die jeweils korrekte
        Schreibweise. Whole-word, case-insensitive. Längere Varianten
        zuerst, damit Mehrwort-Phrasen nicht von kürzeren Substrings
        gestohlen werden."""
        if not text or not self._items:
            return text
        out = text
        for entry in self._items:
            correct = entry["correct"]
            # Längste Variante zuerst (verhindert dass "IQ" früher matcht
            # als "IQ Speaker", wenn beides definiert wäre).
            variants = sorted(entry["variants"], key=len, reverse=True)
            for variant in variants:
                if variant.lower() == correct.lower():
                    continue
                try:
                    pattern = re.compile(
                        r"(?<!\w)" + re.escape(variant) + r"(?!\w)",
                        re.IGNORECASE,
                    )
                    out = pattern.sub(correct, out)
                except re.error:
                    # Defensive: re.escape() macht das eigentlich unmöglich,
                    # aber falls eine Variante doch mal Schrott enthält,
                    # überspringen statt crashen.
                    continue
        return out


# =====================================================================
#  Ollama-Manager: State-Maschine + Worker für Install/Pull/Uninstall.
# =====================================================================

# State-Konstanten - kein Enum, damit die Werte direkt in die Config
# / Logs / UI-Strings wandern können ohne .name/.value-Indirektion.
OLLAMA_NOT_INSTALLED = "not_installed"
OLLAMA_DOWNLOADING   = "downloading"
OLLAMA_INSTALLING    = "installing"
OLLAMA_PULLING       = "pulling_model"
OLLAMA_READY         = "ready"
OLLAMA_PAUSED        = "paused"  # User-Toggle: Backend ist gestoppt
OLLAMA_ERROR         = "error"


class _OllamaCancelled(Exception):
    """User-Abbruch während Install/Pull. Wird vom Worker gefangen, der
    räumt auf und setzt den State zurück."""
    pass

# Modell-Optionen für den Setup-Dropdown. Reihenfolge = UI-Reihenfolge,
# nach Geschwindigkeit auf CPU sortiert (schnellstes oben).
# Speed-Klassen:
#   ⚡⚡⚡ = ~1 s pro Cleanup
#   ⚡⚡  = ~2-3 s
#   ⚡   = ~4-6 s
#   🐢   = >6 s
OLLAMA_MODEL_OPTIONS = [
    ("llama3.2:1b", "llama3.2 1B   ⚡⚡⚡  sehr schnell - Empfohlen für Cleanup"),
    ("llama3.2",    "llama3.2 3B   ⚡⚡    schnell - mehr Qualität"),
    ("phi3",        "phi3 3.8B     ⚡⚡    schnell - Microsoft, kompakt"),
    ("mistral",     "mistral 7B    ⚡      moderat - gut für Deutsch/Englisch"),
    ("llama3.1",    "llama3.1 8B   ⚡      moderat - höhere Qualität"),
    ("gemma2",      "gemma2 9B     🐢    langsam - beste Qualität, hohe RAM-Last"),
]


class OllamaManager(QObject):
    """Steuert Detection / Install / Pull / Uninstall des Ollama-Service.

    Alle Worker laufen in eigenen Threads, GUI-Updates ausschließlich über
    Qt-Signals (queued, automatisch im Main-Thread)."""

    state_changed     = Signal(str)             # neuer State-String
    download_progress = Signal(int, int)        # bytes_done, bytes_total
    install_progress  = Signal(str)             # Status-Text während Install
    pull_progress     = Signal(int, str)        # percent 0-100, Status-Text
    error_message     = Signal(str)             # User-lesbare Fehlermeldung

    HTTP_TIMEOUT = 5.0
    SERVICE_WAIT_SECONDS = 60     # nach Install bis API erreichbar
    DOWNLOAD_CHUNK = 256 * 1024    # 256 KB pro Read aus dem Stream
    DOWNLOAD_TIMEOUT = 120.0       # Sekunden zwischen Reads (urlopen socket-timeout)
    DOWNLOAD_RETRIES = 4           # bei socket.timeout / URLError - mit Range-Resume

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = OLLAMA_NOT_INSTALLED
        self._busy = False  # serialisiert Install/Pull/Uninstall
        self._lock = threading.Lock()
        # User-getriggerter Abbruch während laufendem Worker. Worker prüfen
        # _check_cancel() zwischen Chunks/Lines/Polls.
        self._cancel_event = threading.Event()
        # User-Toggle: wenn False -> Backend wird gekillt, refresh_state
        # startet's nicht neu, Cleanup übersprungen, State = PAUSED.
        # Wird vom IQspeakrApp aus der Config gesetzt, default True.
        self._user_active = True

    # --- Cancel ---
    def cancel(self):
        """Setzt das Cancel-Flag. Der laufende Worker wirft beim nächsten
        _check_cancel() einen _OllamaCancelled und räumt auf."""
        if self._busy:
            log.info("OllamaManager: cancel angefordert")
        self._cancel_event.set()

    def _check_cancel(self):
        if self._cancel_event.is_set():
            raise _OllamaCancelled()

    # --- Ollama-"Integration" (Visibility) ---
    def _remove_ollama_visibility(self):
        """Versteckt Ollama im System: kein Tray-Icon, kein Start-Menü,
        kein Autostart-Eintrag. Idempotent - kann gefahrlos mehrfach
        aufgerufen werden, auch für bestehende Installs."""
        # 1. Tray-Icon-Prozess beenden ("ollama app.exe" ist nur die GUI-Hülle).
        #    "ollama.exe serve" (das Backend) wird nicht angetastet.
        try:
            subprocess.call(
                ["taskkill", "/F", "/IM", "ollama app.exe"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        # 2. Autostart-Reg HKCU\...\Run\Ollama löschen.
        try:
            subprocess.call(
                ["reg", "delete",
                 r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                 "/V", "Ollama", "/F"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        # 3. Start-Menü-Eintrag löschen.
        try:
            sm = os.path.join(
                os.environ.get("APPDATA", ""),
                "Microsoft", "Windows", "Start Menu", "Programs", "Ollama",
            )
            if os.path.isdir(sm):
                import shutil
                shutil.rmtree(sm, ignore_errors=True)
        except Exception:
            pass
        log.info("OllamaManager: Visibility entfernt (Tray, Autostart, Startmenü)")

    def _start_ollama_serve(self):
        """Startet `ollama serve` als detached Background-Prozess - läuft
        weiter wenn IQspeakr beendet wird, aber kein Fenster, kein Tray."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            [OLLAMA_EXE, "serve"],
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("OllamaManager: ollama serve gestartet (detached)")

    def _stop_ollama_serve(self):
        """Killt `ollama.exe`-Prozesse (Backend) per taskkill. Idempotent."""
        try:
            subprocess.call(
                ["taskkill", "/F", "/IM", "ollama.exe"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("OllamaManager: ollama.exe (Backend) beendet")
        except Exception as e:
            log.warning(f"OllamaManager: Backend-Stop fehlgeschlagen: {e}")

    # --- User-Toggle Pause/Resume ---
    def set_user_active(self, active):
        """Wird von SettingsView aufgerufen wenn der User den
        'Ollama-Backend aktiv'-Toggle umschaltet."""
        active = bool(active)
        if active == self._user_active:
            return
        self._user_active = active
        if active:
            log.info("OllamaManager: User hat aktiviert -> refresh")
            self.refresh_state()
        else:
            log.info("OllamaManager: User hat pausiert -> Backend stop")
            self._stop_ollama_serve()
            if os.path.exists(OLLAMA_EXE):
                self._set_state(OLLAMA_PAUSED)
            else:
                self._set_state(OLLAMA_NOT_INSTALLED)

    def is_user_active(self):
        return self._user_active

    # --- State ---
    def state(self):
        return self._state

    def is_ready(self):
        return self._state == OLLAMA_READY

    def is_busy(self):
        return self._busy

    def _set_state(self, s):
        if s == self._state:
            return
        log.info(f"OllamaManager: state {self._state} -> {s}")
        self._state = s
        self.state_changed.emit(s)

    # --- Service-Detection ---
    def _ping_service(self, timeout=None):
        """Healthcheck via /api/tags. Liefert (ok: bool, models: list)."""
        try:
            req = urllib.request.Request(f"{OLLAMA_BASE}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=timeout or self.HTTP_TIMEOUT) as resp:
                if resp.status != 200:
                    return False, []
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in data.get("models", [])]
                return True, models
        except Exception:
            return False, []

    def refresh_state(self, current_model=None):
        """Asynchroner Re-Check des Ollama-Status. Wird beim App-Start und
        nach Konfig-Änderungen aufgerufen. current_model: das in config
        eingestellte Modell - wenn vorhanden + Service läuft -> READY."""
        threading.Thread(
            target=self._refresh_worker,
            args=(current_model,),
            daemon=True,
        ).start()

    def _refresh_worker(self, current_model):
        # User hat das Backend pausiert -> nichts auto-starten.
        if not self._user_active:
            self._stop_ollama_serve()
            if os.path.exists(OLLAMA_EXE):
                self._set_state(OLLAMA_PAUSED)
            else:
                self._set_state(OLLAMA_NOT_INSTALLED)
            return

        # Wenn Ollama installiert ist: Tray-Icon / Autostart / Startmenü
        # einmal stillegen. Idempotent - falls schon weg, no-op.
        if os.path.exists(OLLAMA_EXE):
            self._remove_ollama_visibility()

        ok, _ = self._ping_service()
        if ok:
            self._set_state(OLLAMA_READY)
            return

        if os.path.exists(OLLAMA_EXE):
            # Service down aber installiert -> selbst hochfahren als
            # detached Backend-Prozess.
            try:
                self._start_ollama_serve()
                for _ in range(16):
                    _time.sleep(0.5)
                    ok2, _ = self._ping_service(timeout=1.5)
                    if ok2:
                        self._set_state(OLLAMA_READY)
                        return
            except Exception as e:
                log.warning(f"OllamaManager: serve-Start fehlgeschlagen: {e}")
        self._set_state(OLLAMA_NOT_INSTALLED)

    def has_model(self, name):
        """Prüft synchron ob ein Modell schon gepullt ist."""
        ok, models = self._ping_service()
        if not ok:
            return False
        # Modelle können mit ":latest" o.ae. ankommen.
        prefixes = [name, name + ":"]
        return any(any(m.startswith(p) for p in prefixes) for m in models)

    # --- Install ---
    def install(self, model_name):
        with self._lock:
            if self._busy:
                self.error_message.emit("Eine andere Aktion läuft bereits.")
                return
            self._busy = True
        threading.Thread(
            target=self._install_worker,
            args=(model_name,),
            daemon=True,
        ).start()

    def _install_worker(self, model_name):
        tmp_dir = None
        try:
            self._cancel_event.clear()
            # Schritt 1: Setup-Exe herunterladen.
            self._set_state(OLLAMA_DOWNLOADING)
            tmp_dir = tempfile.mkdtemp(prefix="iqspeakr_ollama_")
            setup_exe = os.path.join(tmp_dir, "OllamaSetup.exe")
            self._download(OLLAMA_INSTALLER_URL, setup_exe)

            # Schritt 2: Silent-Install.
            self._set_state(OLLAMA_INSTALLING)
            self.install_progress.emit("Installation läuft...")
            self._run_installer(setup_exe)

            # Schritt 2b: Ollama unsichtbar machen (kein Tray, kein Auto-
            # start, kein Start-Menü), dann Backend selbst hochfahren.
            self._remove_ollama_visibility()
            try:
                self._start_ollama_serve()
            except Exception as e:
                log.warning(f"_start_ollama_serve nach Install: {e}")

            # Schritt 3: Auf Service warten.
            self.install_progress.emit("Warte auf Ollama-Service...")
            self._wait_for_service(self.SERVICE_WAIT_SECONDS)

            # Schritt 4: Modell pullen.
            self._set_state(OLLAMA_PULLING)
            self._pull(model_name)

            self._set_state(OLLAMA_READY)
        except _OllamaCancelled:
            log.info("OllamaManager: install abgebrochen vom User")
            self.install_progress.emit("Abgebrochen.")
            # State zurück setzen abhängig davon, wie weit wir gekommen sind.
            if os.path.exists(OLLAMA_EXE):
                ok, _ = self._ping_service()
                self._set_state(OLLAMA_READY if ok else OLLAMA_NOT_INSTALLED)
            else:
                self._set_state(OLLAMA_NOT_INSTALLED)
        except Exception as e:
            log.exception("OllamaManager: install fehlgeschlagen")
            self.error_message.emit(str(e))
            self._set_state(OLLAMA_ERROR)
        finally:
            # tempdir aufräumen (Setup.exe ist 1.8 GB).
            if tmp_dir and os.path.isdir(tmp_dir):
                try:
                    import shutil
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
            self._cancel_event.clear()
            with self._lock:
                self._busy = False

    def _download(self, url, dest_path):
        """Robuster Download mit Range-Resume bei socket.timeout / Verbindungs-
        abbruch. Bis zu DOWNLOAD_RETRIES Versuche; jeder Versuch bei
        urlopen mit DOWNLOAD_TIMEOUT-Sekunden Read-Timeout. Wenn der Server
        Range-Requests unterstützt (HTTP 206), wird ab `done` weitergeladen,
        sonst wird die Datei truncated und neu begonnen."""
        import socket as _socket
        max_retries = self.DOWNLOAD_RETRIES
        timeout = self.DOWNLOAD_TIMEOUT
        # Status zum Mitziehen über Retries.
        done = 0
        total = 0
        last_err = None

        for attempt in range(1, max_retries + 1):
            headers = {"User-Agent": "IQspeakr/1.0"}
            if done > 0:
                headers["Range"] = f"bytes={done}-"
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = getattr(resp, "status", 200)
                    cl_str = resp.headers.get("Content-Length") or "0"
                    try:
                        cl = int(cl_str)
                    except ValueError:
                        cl = 0
                    if status == 206 and done > 0:
                        # Server unterstützt Resume - Total = bisheriger
                        # Fortschritt + restliche Bytes.
                        if total <= 0:
                            total = done + cl
                        mode = "ab"
                    else:
                        # Kein Resume - Datei neu beginnen.
                        if status != 206 and done > 0:
                            log.warning(
                                f"OllamaManager: Resume nicht unterstützt "
                                f"(HTTP {status}), starte Download neu."
                            )
                        done = 0
                        total = cl
                        mode = "wb"
                    with open(dest_path, mode) as f:
                        self.download_progress.emit(done, total)
                        while True:
                            self._check_cancel()
                            chunk = resp.read(self.DOWNLOAD_CHUNK)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            self.download_progress.emit(done, total)
                # Erfolg.
                log.info(
                    f"OllamaManager: Setup heruntergeladen: {dest_path} "
                    f"({done} bytes, Versuch {attempt})"
                )
                return
            except (_socket.timeout, urllib.error.URLError, ConnectionError) as e:
                last_err = e
                log.warning(
                    f"OllamaManager: Download-Versuch {attempt}/{max_retries} "
                    f"abgebrochen ({type(e).__name__}: {e}). "
                    f"Bisher {done} bytes, retry in 3s..."
                )
                if attempt < max_retries:
                    self.install_progress.emit(
                        f"Download abgebrochen, Versuch {attempt + 1}/{max_retries}..."
                    )
                    _time.sleep(3.0)
                continue
        raise RuntimeError(
            f"Download nach {max_retries} Versuchen fehlgeschlagen: {last_err}"
        )

    def _run_installer(self, setup_exe):
        # Inno-Setup-Flags - laut https://jrsoftware.org/ishelp/index.php?topic=setupcmdline
        # /VERYSILENT: keine UI, /SUPPRESSMSGBOXES: keine Dialoge,
        # /NORESTART: kein Reboot-Prompt am Ende.
        # Popen+poll-Schleife (statt subprocess.call), damit wir bei
        # User-Cancel den Installer-Prozess terminieren können.
        cmd = [setup_exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
        log.info(f"OllamaManager: starte Installer {cmd}")
        proc = subprocess.Popen(
            cmd,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            while proc.poll() is None:
                if self._cancel_event.is_set():
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise _OllamaCancelled()
                _time.sleep(0.3)
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        rc = proc.returncode
        if rc != 0:
            raise RuntimeError(f"OllamaSetup.exe Exit-Code {rc}")
        log.info("OllamaManager: Installer fertig (rc=0)")

    def _wait_for_service(self, timeout_seconds):
        deadline = _time.time() + timeout_seconds
        while _time.time() < deadline:
            ok, _ = self._ping_service(timeout=2.0)
            if ok:
                return
            _time.sleep(1.0)
        raise RuntimeError("Ollama-Service nicht erreichbar nach Install (Timeout)")

    def _pull(self, model_name):
        """POST /api/pull mit stream=true. Parst JSONL-Stream und emittet
        pull_progress(percent, status)."""
        body = json.dumps({"name": model_name, "stream": True}).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/pull",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_pct = -1
        with urllib.request.urlopen(req, timeout=None) as resp:
            buf = b""
            while True:
                self._check_cancel()
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    status = msg.get("status", "")
                    total = msg.get("total")
                    completed = msg.get("completed")
                    if total and completed and total > 0:
                        pct = max(0, min(100, int(completed * 100 / total)))
                        if pct != last_pct:
                            last_pct = pct
                            self.pull_progress.emit(pct, status)
                    elif status:
                        self.pull_progress.emit(last_pct if last_pct >= 0 else 0, status)
                    if msg.get("error"):
                        raise RuntimeError(msg["error"])

    def pull_model(self, model_name):
        """Public: zieht ein zusätzliches Modell, vorausgesetzt Ollama
        läuft schon (state == ready)."""
        with self._lock:
            if self._busy:
                self.error_message.emit("Eine andere Aktion läuft bereits.")
                return
            self._busy = True
        threading.Thread(
            target=self._pull_only_worker,
            args=(model_name,),
            daemon=True,
        ).start()

    def _pull_only_worker(self, model_name):
        try:
            self._cancel_event.clear()
            self._set_state(OLLAMA_PULLING)
            self._pull(model_name)
            self._set_state(OLLAMA_READY)
        except _OllamaCancelled:
            log.info("OllamaManager: pull abgebrochen vom User")
            self.install_progress.emit("Abgebrochen.")
            ok, _ = self._ping_service()
            self._set_state(OLLAMA_READY if ok else OLLAMA_NOT_INSTALLED)
        except Exception as e:
            log.exception("OllamaManager: pull fehlgeschlagen")
            self.error_message.emit(str(e))
            self._set_state(OLLAMA_ERROR)
        finally:
            self._cancel_event.clear()
            with self._lock:
                self._busy = False

    # --- Uninstall ---
    def uninstall(self):
        with self._lock:
            if self._busy:
                self.error_message.emit("Eine andere Aktion läuft bereits.")
                return
            self._busy = True
        threading.Thread(target=self._uninstall_worker, daemon=True).start()

    def _uninstall_worker(self):
        try:
            self.install_progress.emit("Beende Ollama-Prozesse...")
            for proc_name in ("ollama app.exe", "ollama.exe"):
                try:
                    subprocess.call(
                        ["taskkill", "/F", "/IM", proc_name],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass
            _time.sleep(1.0)

            if not os.path.exists(OLLAMA_UNINSTALLER):
                # Inno-Uninstaller fehlt - vielleicht manuell installiert?
                # Trotzdem als entfernt behandeln, der Service ist eh tot.
                self._set_state(OLLAMA_NOT_INSTALLED)
                return
            self.install_progress.emit("Deinstalliere Ollama...")
            cmd = [OLLAMA_UNINSTALLER, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
            rc = subprocess.call(
                cmd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # Inno-Uninstaller forkt sich oft selbst und rc != 0 ist nicht
            # zwangsläufig ein Fehler - wir verifizieren über Service-Status.
            log.info(f"OllamaManager: Uninstaller rc={rc}")
            # Bis zu 30s warten, bis OLLAMA_EXE weg ist.
            for _ in range(30):
                if not os.path.exists(OLLAMA_EXE):
                    break
                _time.sleep(1.0)
            self._set_state(OLLAMA_NOT_INSTALLED)
        except Exception as e:
            log.exception("OllamaManager: uninstall fehlgeschlagen")
            self.error_message.emit(str(e))
            self._set_state(OLLAMA_ERROR)
        finally:
            with self._lock:
                self._busy = False


# --- Tray-Icon-Helfer (Qt-Painter statt PIL) ---

def _make_icon_pixmap(state):
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    color = {
        "ready": QColor(80, 80, 80),
        "rec":   QColor(220, 40, 40),
        "busy":  QColor(230, 140, 30),
    }.get(state, QColor(80, 80, 80))
    p.setBrush(color)
    p.setPen(QColor(0, 0, 0))
    p.drawEllipse(6, 6, 52, 52)
    p.end()
    return pm


# =====================================================================
#  Hotkey-Recorder: Dialog, der Tastendrücke live aufnimmt.
# =====================================================================

class HotkeyRecorderDialog(QDialog):
    """Modaler Dialog zum Aufnehmen einer Tastenkombination.

    Der User druckt einfach die gewünschte Kombi (z.B. Strg+Umschalt), lässt
    los, der Dialog zeigt das Ergebnis und speichert es beim Klick auf
    "Speichern"."""

    _MOD_ORDER = ("ctrl", "alt", "shift", "win")

    # Heuristik: Kombis die wir als System-/App-kritisch flaggen. Kein
    # hartes Verbot, nur Confirm vor dem Speichern.
    _CONFLICT_COMBOS = {
        "ctrl+c":     "Kopieren",
        "ctrl+v":     "Einfügen (wird intern für Auto-Paste benutzt!)",
        "ctrl+x":     "Ausschneiden",
        "ctrl+z":     "Rückgängig",
        "ctrl+y":     "Wiederholen",
        "ctrl+s":     "Speichern",
        "ctrl+a":     "Alles auswählen",
        "ctrl+f":     "Suchen",
        "alt+tab":    "Fenster wechseln",
        "alt+f4":     "Fenster schließen",
        "win+l":      "Computer sperren",
        "win+d":      "Desktop anzeigen",
        "win+e":      "Explorer öffnen",
    }

    def __init__(self, parent=None, initial=""):
        super().__init__(parent)
        self.setWindowTitle("Tastenkombination ändern")
        self.setModal(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.resize(520, 280)
        if os.path.exists(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))

        self._mods = set()
        self._key = None
        self._capture_complete = False
        self._captured = (initial or "").strip().lower()

        # Poll-Timer als Workaround für einen Windows-Qt-Quirk: modale
        # Dialoge bekommen für Modifier-Keys (Ctrl/Shift/Alt/Win) nicht
        # zuverlässig keyReleaseEvent. queryKeyboardModifiers() liest den
        # echten OS-Tastaturzustand unabhängig von Qt's Event-Lieferung.
        # Sobald keine Modifier mehr gedrückt sind, finalizen wir den
        # Capture. Quelle: https://doc.qt.io/qt-6/qguiapplication.html
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(30)
        self._poll_timer.timeout.connect(self._poll_release)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(14)

        head = QLabel("Drücke die gewünschte Tastenkombination")
        head_font = head.font()
        head_font.setPointSizeF(head_font.pointSizeF() + 4)
        head_font.setBold(True)
        head.setFont(head_font)
        head.setStyleSheet(f"color: {THEME_TEXT};")
        layout.addWidget(head)

        info = QLabel(
            "Mindestens ein Modifier (Strg, Umschalt, Alt, Win) erforderlich. "
            "Optional dazu ein Buchstabe, Leertaste, Eingabe, Tab oder F1-F12."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
        layout.addWidget(info)

        # Display-Frame als Card mit Akzent-Border, damit das Capture
        # visuell heraussticht.
        display_frame = QFrame()
        display_frame.setObjectName("HotkeyDisplay")
        display_frame.setStyleSheet(
            f"#HotkeyDisplay {{"
            f" background: {THEME_BG_INPUT};"
            f" border: 1px dashed {THEME_BORDER_HOVER};"
            f" border-radius: 10px;"
            f" min-height: 64px;"
            f"}}"
        )
        df_layout = QVBoxLayout(display_frame)
        df_layout.setContentsMargins(16, 14, 16, 14)
        self._display = QLabel()
        font = self._display.font()
        font.setPointSizeF(font.pointSizeF() + 6)
        font.setBold(True)
        self._display.setFont(font)
        self._display.setAlignment(Qt.AlignCenter)
        self._display.setMinimumHeight(36)
        self._display.setStyleSheet(f"color: {THEME_TEXT};")
        df_layout.addWidget(self._display)
        layout.addWidget(display_frame)

        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet(f"color: {THEME_DANGER}; font-size: 11px;")
        self._error_lbl.setVisible(False)
        layout.addWidget(self._error_lbl)

        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("Abbrechen")
        self._cancel_btn.setMinimumHeight(34)
        self._cancel_btn.setMinimumWidth(110)
        self._cancel_btn.setAutoDefault(False)
        self._cancel_btn.setDefault(False)
        self._cancel_btn.setFocusPolicy(Qt.NoFocus)
        self._cancel_btn.clicked.connect(self.reject)

        self._ok_btn = QPushButton("Speichern")
        self._ok_btn.setProperty("role", "primary")
        self._ok_btn.setMinimumHeight(34)
        self._ok_btn.setMinimumWidth(140)
        self._ok_btn.setAutoDefault(False)
        self._ok_btn.setDefault(False)
        self._ok_btn.setFocusPolicy(Qt.NoFocus)
        self._ok_btn.clicked.connect(self._on_save_clicked)

        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

        self._refresh_display()

    def _build_combo_str(self):
        parts = [m for m in self._MOD_ORDER if m in self._mods]
        if self._key:
            parts.append(self._key)
        return "+".join(parts)

    def _has_modifier(self, combo_str):
        parts = [p.strip() for p in (combo_str or "").lower().split("+") if p.strip()]
        return any(p in _MODIFIER_ALIASES for p in parts)

    def _refresh_display(self):
        live = self._build_combo_str()
        shown = live or self._captured
        if shown:
            self._display.setText(hotkey_display(shown))
            self._display.setStyleSheet(f"color: {THEME_TEXT};")
        else:
            self._display.setText("(drücke eine Taste)")
            self._display.setStyleSheet(f"color: {THEME_TEXT_MUTED};")

        valid = bool(self._captured) and self._has_modifier(self._captured)
        self._ok_btn.setEnabled(valid)
        if self._captured and not self._has_modifier(self._captured):
            self._error_lbl.setText("Mindestens ein Modifier (Strg, Umschalt, Alt, Win) erforderlich.")
            self._error_lbl.setVisible(True)
        else:
            self._error_lbl.setVisible(False)

    def _on_save_clicked(self):
        combo = self._captured
        if not combo or not self._has_modifier(combo):
            return
        # Konflikt-Heuristik: bekannte System-Standards mit Confirm-Dialog
        # überschreiben lassen. Kein Hard-Block.
        warn = self._CONFLICT_COMBOS.get(combo)
        if warn:
            confirm = QMessageBox.question(
                self,
                "Konflikt mit Standard-Shortcut",
                f"\"{hotkey_display(combo)}\" ist normalerweise reserviert für:\n"
                f"  {warn}\n\n"
                "Trotzdem als Hotkey verwenden?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        self.accept()

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        # Neuer Press nach einem fertigen Capture -> reset und neu aufnehmen
        if self._capture_complete:
            self._mods = set()
            self._key = None
            self._capture_complete = False

        qt_key = event.key()
        # Modifier-Set akkumulieren - queryKeyboardModifiers() liest den
        # echten OS-Tastaturzustand. WICHTIG: kein int()-Cast hier, der
        # Rückgabetyp ist eine Enum-Flag und kein Integer (PySide6).
        qm = QGuiApplication.queryKeyboardModifiers()
        if qm & Qt.ControlModifier: self._mods.add("ctrl")
        if qm & Qt.ShiftModifier:   self._mods.add("shift")
        if qm & Qt.AltModifier:     self._mods.add("alt")
        if qm & Qt.MetaModifier:    self._mods.add("win")

        if qt_key in _QT_MOD_TO_NAME:
            # Just-pressed Modifier ist evtl. noch nicht im OS-Cache -
            # explizit dazunehmen.
            self._mods.add(_QT_MOD_TO_NAME[qt_key])
        elif qt_key in _QT_NAMED_KEYS:
            self._key = _QT_NAMED_KEYS[qt_key]
        else:
            t = (event.text() or "").lower()
            if len(t) == 1 and t.isalnum():
                self._key = t

        self._refresh_display()
        # Poll-Timer (re)starten - läuft so lange, bis User alle Modifier
        # losgelassen hat. Workaround für fehlende keyRelease-Events unter
        # Windows-Modal (siehe __init__-Kommentar).
        self._poll_timer.start()
        event.accept()

    def keyReleaseEvent(self, event):
        # Wir verlassen uns NICHT auf keyReleaseEvent - Modifier-Releases
        # kommen unter Windows-Modal-Dialogen nicht zuverlässig an.
        # Finalisierung läuft via _poll_release-Timer.
        event.accept()

    def _poll_release(self):
        """Wird via QTimer alle 30ms nach einem keyPress gefeuert.
        queryKeyboardModifiers() spiegelt den echten OS-Tastaturzustand
        - sobald keine Modifier mehr gedrückt sind, ist die Combo komplett."""
        qm = QGuiApplication.queryKeyboardModifiers()
        any_mod_down = bool(
            (qm & Qt.ControlModifier) or (qm & Qt.ShiftModifier)
            or (qm & Qt.AltModifier) or (qm & Qt.MetaModifier)
        )
        if not any_mod_down:
            self._poll_timer.stop()
            combo = self._build_combo_str()
            if combo:
                self._captured = combo
                self._capture_complete = True
                self._refresh_display()

    def result_combo(self):
        return self._captured


# =====================================================================
#  Detail-Dialog für einen History-Eintrag.
# =====================================================================

class TranscriptDetailDialog(QDialog):
    def __init__(self, ts, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Transkript")
        self.resize(620, 420)
        if os.path.exists(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        ts_str = _time.strftime("%d.%m.%Y  %H:%M:%S", _time.localtime(ts))
        head = QLabel(ts_str)
        head.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(head)

        edit = QPlainTextEdit(self)
        edit.setReadOnly(True)
        edit.setPlainText(text)
        layout.addWidget(edit, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._copy_btn = QPushButton("In Zwischenablage kopieren")
        self._copy_btn.setProperty("role", "primary")
        self._copy_btn.setMinimumHeight(32)
        self._copy_btn.clicked.connect(lambda: self._copy(text))
        close_btn = QPushButton("Schließen")
        close_btn.setMinimumHeight(32)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._copy_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _copy(self, text):
        try:
            pyperclip.copy(text)
            self._copy_btn.setText("Kopiert")
        except Exception as e:
            log.warning(f"TranscriptDetailDialog: copy fehlgeschlagen: {e}")
            self._copy_btn.setText("Fehler")


# =====================================================================
#  Home-View: Liste der letzten Transkripte.
# =====================================================================

class HistoryEntryCard(QFrame):
    """Eine Row in der History-Liste. Zeigt Timestamp + vollen Text + Copy-
    Button. Border-bottom wird vom Layout-Manager gesteuert (last-Property)."""

    def __init__(self, ts, text, parent=None):
        super().__init__(parent)
        self._text = text or ""
        self.setObjectName("HistoryRow")
        self.setProperty("last", False)
        self._apply_qss()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(14)

        body = QVBoxLayout()
        body.setSpacing(6)

        ts_str = _time.strftime("%d.%m.%Y  %H:%M", _time.localtime(ts or 0))
        ts_lbl = QLabel(ts_str)
        ts_lbl.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 11px;")
        body.addWidget(ts_lbl)

        text_lbl = QLabel(self._text)
        text_lbl.setWordWrap(True)
        text_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_lbl.setStyleSheet(f"color: {THEME_TEXT}; font-size: 13px;")
        body.addWidget(text_lbl)

        outer.addLayout(body, 1)

        # Copy-Button rechts oben in der Row, vertikal an top alignt.
        btn_col = QVBoxLayout()
        btn_col.setSpacing(0)
        btn_col.addStretch(0)
        self._copy_btn = QPushButton()
        self._copy_btn.setIcon(_lucide_icon(_LUCIDE_COPY, 16, THEME_TEXT_SECONDARY))
        self._copy_btn.setToolTip("Text kopieren")
        self._copy_btn.setFixedSize(32, 30)
        self._copy_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._copy_btn.clicked.connect(self._on_copy)
        btn_col.addWidget(self._copy_btn, 0, Qt.AlignTop)
        btn_col.addStretch(1)
        outer.addLayout(btn_col, 0)

    def _apply_qss(self):
        self.setStyleSheet(
            f"QFrame#HistoryRow {{"
            f" background: transparent;"
            f" border-bottom: 1px solid {THEME_BORDER_SOFT};"
            f"}}"
            f"QFrame#HistoryRow:hover {{"
            f" background: rgba(255, 255, 255, 0.04);"
            f"}}"
            f"QFrame#HistoryRow[last=\"true\"] {{"
            f" border-bottom: 0;"
            f"}}"
        )

    def set_last(self, last):
        self.setProperty("last", bool(last))
        self.style().unpolish(self)
        self.style().polish(self)

    def _on_copy(self):
        try:
            pyperclip.copy(self._text)
        except Exception as e:
            log.warning(f"HistoryEntryCard: copy fehlgeschlagen: {e}")
            return
        # kurzes visuelles Feedback - 1 s lang Check-Icon, dann zurück.
        self._copy_btn.setIcon(_lucide_icon(_LUCIDE_CHECK, 16, THEME_SUCCESS))
        QTimer.singleShot(1000, self._reset_copy_icon)

    def _reset_copy_icon(self):
        self._copy_btn.setIcon(_lucide_icon(_LUCIDE_COPY, 16, THEME_TEXT_SECONDARY))


class HomeView(QWidget):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 32, 36, 28)
        layout.setSpacing(6)

        title = QLabel("History")
        title.setProperty("role", "h1")
        layout.addWidget(title)

        sub = QLabel(f"Die letzten {HISTORY_MAX} Transkripte. Älteste fliegen automatisch raus.")
        sub.setProperty("role", "sub")
        layout.addWidget(sub)
        layout.addSpacing(18)

        # Card-Container für die Liste der Einträge - wird gefüllt
        # von refresh().
        self._card = QFrame()
        self._card.setObjectName("HistoryCard")
        self._card.setStyleSheet(
            f"#HistoryCard {{"
            f" background: {THEME_BG_CARD};"
            f" border: 1px solid {THEME_BORDER};"
            f" border-radius: 12px;"
            f"}}"
        )

        # ScrollArea umschließt den Card-Container, damit lange History
        # vertikal scrollbar wird ohne dass die Card-Border bricht.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }"
                                   "QScrollArea > QWidget > QWidget { background: transparent; }")

        self._card_layout = QVBoxLayout(self._card)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(0)

        self._scroll.setWidget(self._card)
        layout.addWidget(self._scroll, 1)

        self._entries = []
        self.refresh()
        self.app.history.changed.connect(self.refresh)

    def refresh(self):
        # Alle vorhandenen Cards rauswerfen.
        while self._card_layout.count():
            item = self._card_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        self._entries = self.app.history.items()
        if not self._entries:
            empty = QLabel("Noch keine Aufnahmen.\nHalte deinen Hotkey gedrückt und sprich.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {THEME_TEXT_MUTED}; padding: 60px 20px;")
            self._card_layout.addWidget(empty)
            return

        last_idx = len(self._entries) - 1
        for i, entry in enumerate(self._entries):
            card = HistoryEntryCard(
                entry.get("ts", 0),
                entry.get("text", ""),
                parent=self._card,
            )
            if i == last_idx:
                card.set_last(True)
            self._card_layout.addWidget(card)
        self._card_layout.addStretch(1)


# =====================================================================
#  Dashboard-View: Stat-Cards + 12-Wochen-Activity-Heatmap.
# =====================================================================

class StatCard(QFrame):
    """Eine Kennzahl-Card: großer Wert, Label, optional Sub-Label."""

    def __init__(self, label, value, sub=None, parent=None):
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setStyleSheet(
            f"#StatCard {{"
            f" background: {THEME_BG_CARD};"
            f" border: 1px solid {THEME_BORDER};"
            f" border-radius: 12px;"
            f"}}"
        )
        self.setMinimumHeight(120)

        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 18)
        v.setSpacing(6)

        self._value_lbl = QLabel(str(value))
        f = self._value_lbl.font()
        f.setPointSizeF(f.pointSizeF() + 14)
        f.setBold(True)
        self._value_lbl.setFont(f)
        self._value_lbl.setStyleSheet(f"color: {THEME_TEXT};")
        v.addWidget(self._value_lbl)

        self._label_lbl = QLabel(label)
        self._label_lbl.setStyleSheet(f"color: {THEME_TEXT_SECONDARY}; font-size: 12px;")
        v.addWidget(self._label_lbl)

        self._sub_lbl = QLabel(sub or "")
        self._sub_lbl.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 11px;")
        self._sub_lbl.setVisible(bool(sub))
        v.addWidget(self._sub_lbl)

        v.addStretch(1)

    def set_value(self, value):
        self._value_lbl.setText(str(value))

    def set_sub(self, sub):
        self._sub_lbl.setText(sub or "")
        self._sub_lbl.setVisible(bool(sub))


class _ResponsiveCardGrid(QWidget):
    """Container der Cards in einem dynamischen Grid anordnet. Spalten-
    Anzahl wird in resizeEvent abhängig von der eigenen Breite gewählt
    (Breakpoints siehe BREAKPOINTS). Äquivalent zu CSS
    `grid-template-columns: repeat(auto-fit, minmax(MIN, 1fr))` — Qt
    hat das nativ nicht.

    Die Cards strecken sich auf gleiche Spaltenbreite. Der Reflow
    passiert nur wenn sich die Spalten-Anzahl tatsächlich ändert,
    sonst Flickern."""

    # Map: Mindest-Breite des Grids -> Spalten-Anzahl. Große Schwelle
    # zuerst (wir nehmen die erste passende). Die konkreten Werte werden
    # vom Caller gesetzt (Style: 4/2/1, Dashboard: 3/2/1).
    DEFAULT_BREAKPOINTS = ((1100, 4), (800, 2), (0, 1))

    def __init__(self, breakpoints=None, gap=14, parent=None):
        super().__init__(parent)
        self._breakpoints = tuple(breakpoints or self.DEFAULT_BREAKPOINTS)
        self._cards = []
        self._current_cols = -1
        self._grid = QGridLayout(self)
        self._grid.setSpacing(gap)
        self._grid.setContentsMargins(0, 0, 0, 0)

    def add_card(self, widget):
        self._cards.append(widget)
        # Erst-Anordnung: aktuelle Breite könnte 0 sein wenn das Widget
        # noch nicht im Layout ist - dann nehmen wir die maximale Spalten-
        # Anzahl als Default, der echte Reflow kommt im ersten resizeEvent.
        if self._current_cols < 0:
            self._current_cols = self._breakpoints[0][1]
        self._reflow(force=True)

    def _columns_for_width(self, w):
        for thresh, cols in self._breakpoints:
            if w >= thresh:
                return cols
        return 1

    def _reflow(self, force=False):
        cols = self._columns_for_width(self.width())
        if not force and cols == self._current_cols:
            return
        self._current_cols = cols
        # Alle Widgets aus dem Grid lösen (nicht zerstören).
        for c in self._cards:
            self._grid.removeWidget(c)
        # Neu platzieren.
        for i, card in enumerate(self._cards):
            row = i // cols
            col = i % cols
            self._grid.addWidget(card, row, col)
        # Alle Spalten gleichmäßig stretchen, leere Spalten zurücksetzen.
        max_cols = max(c for _, c in self._breakpoints)
        for ci in range(max_cols):
            self._grid.setColumnStretch(ci, 1 if ci < cols else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()


class HeatmapWidget(QWidget):
    """12 Wochen Activity-Heatmap, GitHub-Style. Mouseover setzt Tooltip
    'X Aufnahmen am DD.MM.YYYY'. Spalten = Wochen (Mo bis So), Zeilen =
    Wochentage. Die rechte Spalte enthält die aktuelle (oft unvollständige)
    Woche, die linke die älteste der 12-Wochen-Range."""

    WEEKS = 12
    DAYS = 7
    CELL = 14
    GAP = 4
    LEFT_PAD = 36     # Platz für Wochentag-Labels
    TOP_PAD = 22      # Platz für Monatslabels
    LEGEND_HEIGHT = 26

    # 5 Stufen, von "leer" bis "voll" - voll = Akzent.
    LEVEL_COLORS = [
        QColor("#22252D"),                # 0 - leicht heller als BG_CARD damit man's sieht
        QColor(99, 102, 241,  60),        # 1 (alpha)
        QColor(99, 102, 241, 110),        # 2-3
        QColor(99, 102, 241, 175),        # 4-7
        QColor(99, 102, 241, 255),        # 8+
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._counts = {}     # date -> int
        self._cells = []      # [(QRect, date, count), ...] für Mouse-Lookup
        self.setMouseTracking(True)
        # Höhe = Top-Pad + 7 Zeilen + 6 Gaps + Legenden-Bereich
        h = self.TOP_PAD + self.DAYS * self.CELL + (self.DAYS - 1) * self.GAP + self.LEGEND_HEIGHT
        self.setMinimumHeight(h)
        # Breite reserviert sich an Layout-Stretch, paint nutzt was da ist.

    def set_counts(self, counts):
        self._counts = dict(counts or {})
        self.update()

    def _level_for_count(self, n):
        if n <= 0: return 0
        if n == 1: return 1
        if n <= 3: return 2
        if n <= 7: return 3
        return 4

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        today = date.today()
        # Letztes Sonntag der Anzeige = Ende der aktuellen Woche.
        # Mo=0..So=6. Wir wollen Spalten von links (alteste Woche) nach
        # rechts (jüngste Woche, endet am letzten Sonntag).
        end_sunday = today + timedelta(days=(6 - today.weekday()))
        start_monday = end_sunday - timedelta(days=self.WEEKS * 7 - 1)

        self._cells = []

        # Wochentag-Labels (nur Mo, Mi, Fr - sonst zu voll).
        p.setPen(QColor(THEME_TEXT_MUTED))
        f = p.font(); f.setPointSize(8); p.setFont(f)
        weekday_names = {0: "Mo", 2: "Mi", 4: "Fr"}
        for wd, name in weekday_names.items():
            y = self.TOP_PAD + wd * (self.CELL + self.GAP) + self.CELL - 3
            p.drawText(2, y, name)

        # Monats-Labels: einmal pro Monat, dort wo eine neue Spalte
        # einen Monatsanfang (Tag 1-7 = erste Woche) enthält.
        last_month = None
        for col in range(self.WEEKS):
            week_start = start_monday + timedelta(days=col * 7)
            # Prüfe ob in dieser Woche der Monatsanfang liegt
            for d in range(7):
                day = week_start + timedelta(days=d)
                if day.day <= 7 and day.month != last_month:
                    last_month = day.month
                    x = self.LEFT_PAD + col * (self.CELL + self.GAP)
                    p.drawText(x, self.TOP_PAD - 8,
                               ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
                                "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"][day.month - 1])
                    break

        # Zellen
        p.setPen(Qt.NoPen)
        for col in range(self.WEEKS):
            for row in range(self.DAYS):
                day = start_monday + timedelta(days=col * 7 + row)
                if day > today:
                    # Zukunfts-Tag - leerer, aber transparent gezeichnet
                    color = QColor(0, 0, 0, 0)
                else:
                    n = self._counts.get(day, 0)
                    color = self.LEVEL_COLORS[self._level_for_count(n)]
                x = self.LEFT_PAD + col * (self.CELL + self.GAP)
                y = self.TOP_PAD + row * (self.CELL + self.GAP)
                rect_x, rect_y = x, y
                p.setBrush(color)
                p.drawRoundedRect(rect_x, rect_y, self.CELL, self.CELL, 3, 3)
                if day <= today:
                    self._cells.append((
                        (rect_x, rect_y, self.CELL, self.CELL),
                        day,
                        self._counts.get(day, 0),
                    ))

        # Legende rechts unten: "Weniger [▢▢▢▢▢] Mehr"
        legend_y = self.TOP_PAD + self.DAYS * self.CELL + (self.DAYS - 1) * self.GAP + 12
        legend_w = 5 * self.CELL + 4 * 3 + 100
        legend_x = self.width() - legend_w - 4
        p.setPen(QColor(THEME_TEXT_MUTED))
        f = p.font(); f.setPointSize(8); p.setFont(f)
        p.drawText(legend_x, legend_y + self.CELL - 3, "Weniger")
        p.setPen(Qt.NoPen)
        cx = legend_x + 50
        for lvl in range(5):
            p.setBrush(self.LEVEL_COLORS[lvl])
            p.drawRoundedRect(cx, legend_y, self.CELL, self.CELL, 3, 3)
            cx += self.CELL + 3
        p.setPen(QColor(THEME_TEXT_MUTED))
        p.drawText(cx + 4, legend_y + self.CELL - 3, "Mehr")
        p.end()

    def mouseMoveEvent(self, ev):
        x, y = ev.position().x(), ev.position().y()
        for (rx, ry, rw, rh), d, n in self._cells:
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                self.setToolTip(f"{n} {'Aufnahme' if n == 1 else 'Aufnahmen'} am {d.strftime('%d.%m.%Y')}")
                return
        self.setToolTip("")


class DashboardView(QWidget):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(36, 32, 36, 28)
        v.setSpacing(20)

        title = QLabel("Dashboard")
        title.setProperty("role", "h1")
        v.addWidget(title)

        sub = QLabel("Deine Nutzungsstatistiken.")
        sub.setProperty("role", "sub")
        v.addWidget(sub)
        v.addSpacing(10)

        # Empty-Page (Stack) wechselt zwischen "noch keine Daten" und Stats.
        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)

        # --- Empty-State ---
        empty_page = QWidget()
        ep = QVBoxLayout(empty_page)
        ep.setContentsMargins(0, 0, 0, 0)
        ep.addStretch(1)
        empty_lbl = QLabel(
            "Noch keine Daten.\nMach deine erste Aufnahme um Statistiken zu sehen."
        )
        empty_lbl.setAlignment(Qt.AlignCenter)
        empty_lbl.setWordWrap(True)
        empty_lbl.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 13px;")
        ep.addWidget(empty_lbl)
        ep.addStretch(2)
        self._stack.addWidget(empty_page)

        # --- Stats-Page ---
        stats_page = QWidget()
        sp = QVBoxLayout(stats_page)
        sp.setContentsMargins(0, 0, 0, 0)
        sp.setSpacing(20)

        # Stat-Cards-Grid: responsive 3/2/1 Spalten (Breakpoints 1100/800).
        self._wpm_card = StatCard("Wörter pro Minute", "—")
        self._words_card = StatCard("Wörter insgesamt", "0")
        self._streak_card = StatCard("Tage Streak", "0", sub="Längster Streak: 0 Tage")
        cards_grid = _ResponsiveCardGrid(
            breakpoints=((1100, 3), (800, 2), (0, 1)), gap=16,
        )
        for c in (self._wpm_card, self._words_card, self._streak_card):
            cards_grid.add_card(c)
        sp.addWidget(cards_grid)

        # Heatmap als eigene Card.
        heat_card = QFrame()
        heat_card.setObjectName("HeatCard")
        heat_card.setStyleSheet(
            f"#HeatCard {{"
            f" background: {THEME_BG_CARD};"
            f" border: 1px solid {THEME_BORDER};"
            f" border-radius: 12px;"
            f"}}"
        )
        hc = QVBoxLayout(heat_card)
        hc.setContentsMargins(20, 18, 20, 18)
        hc.setSpacing(10)
        hc_title = QLabel("Aktivität (letzte 12 Wochen)")
        hc_title.setStyleSheet(f"color: {THEME_TEXT}; font-weight: 600;")
        hc.addWidget(hc_title)
        self._heatmap = HeatmapWidget()
        hc.addWidget(self._heatmap)
        sp.addWidget(heat_card)
        sp.addStretch(1)

        self._stack.addWidget(stats_page)

        self.refresh()
        # Updates wenn neue Aufnahme reinkommt.
        self.app.stats.changed.connect(self.refresh)

    def refresh(self):
        if self.app.stats.is_empty():
            self._stack.setCurrentIndex(0)
            return
        self._stack.setCurrentIndex(1)

        # WPM
        wpm = self.app.stats.avg_wpm()
        if wpm is None:
            self._wpm_card.set_value("—")
            self._wpm_card.set_sub("Brauchen Aufnahmen mit bekannter Dauer")
        else:
            self._wpm_card.set_value(f"{wpm:.0f}")
            self._wpm_card.set_sub(None)

        # Wörter total - Tausender mit Punkt (DE-Konvention).
        total = self.app.stats.total_words()
        self._words_card.set_value(f"{total:,}".replace(",", "."))

        # Streak
        cur = self.app.stats.current_streak()
        longest = self.app.stats.longest_streak()
        self._streak_card.set_value(str(cur))
        self._streak_card.set_sub(
            f"Laengster Streak: {longest} {'Tag' if longest == 1 else 'Tage'}"
        )

        # Heatmap
        counts = self.app.stats.daily_counts(HeatmapWidget.WEEKS * 7)
        self._heatmap.set_counts(counts)


# =====================================================================
#  Style-View: vier Karten + Individuell-Editor + Sperr-Bildschirm.
# =====================================================================

class StyleCard(QFrame):
    """Eine klickbare Karte für einen Style. Selected-State steuert die
    Border-Farbe über stylesheet-property 'selected'."""

    clicked = Signal(str)  # style key

    CARD_QSS = (
        f"#StyleCard {{"
        f" background: {THEME_BG_CARD};"
        f" border: 1px solid {THEME_BORDER};"
        f" border-radius: 12px;"
        f"}}"
        f"#StyleCard:hover {{"
        f" border-color: {THEME_BORDER_HOVER};"
        f"}}"
        f"#StyleCard[selected=\"true\"] {{"
        f" border: 1px solid {THEME_ACCENT};"
        f" background: {THEME_ACCENT_SOFT};"
        f"}}"
    )

    def __init__(self, key, title, sample_input, sample_output, parent=None):
        super().__init__(parent)
        self.key = key
        self.setProperty("selected", False)
        self.setObjectName("StyleCard")
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setStyleSheet(self.CARD_QSS)
        self.setMinimumWidth(190)

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 18)
        v.setSpacing(10)

        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        head = QLabel(title)
        f = head.font(); f.setPointSizeF(f.pointSizeF() + 2); f.setBold(True); head.setFont(f)
        head.setStyleSheet(f"color: {THEME_TEXT};")
        head_row.addWidget(head)
        head_row.addStretch(1)

        self._check = QLabel("●")
        self._check.setStyleSheet(f"color: {THEME_ACCENT}; font-size: 14px;")
        self._check.setVisible(False)
        head_row.addWidget(self._check)
        v.addLayout(head_row)

        sample_in_lbl = QLabel(f"Diktat: {sample_input}")
        sample_in_lbl.setWordWrap(True)
        sample_in_lbl.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 11px;")
        v.addWidget(sample_in_lbl)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {THEME_BORDER};")
        v.addWidget(sep)

        self.sample_out_lbl = QLabel(sample_output)
        self.sample_out_lbl.setWordWrap(True)
        self.sample_out_lbl.setStyleSheet(f"color: {THEME_TEXT};")
        v.addWidget(self.sample_out_lbl, 1)

    def set_selected(self, selected):
        self.setProperty("selected", bool(selected))
        self._check.setVisible(bool(selected))
        # Stylesheet neu anwenden, sonst greift der property-selector nicht.
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.key)
        super().mousePressEvent(event)


class StyleView(QWidget):
    def __init__(self, app, on_jump_to_settings, parent=None):
        super().__init__(parent)
        self.app = app
        self._on_jump_to_settings = on_jump_to_settings
        self._cards = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack)

        # --- Page 0: Sperr-Hinweis (Ollama nicht installiert) ---
        lock_page = QWidget()
        lock_layout = QVBoxLayout(lock_page)
        lock_layout.setContentsMargins(36, 32, 36, 28)
        lock_layout.addStretch(1)

        lock_box = QFrame()
        lock_box.setObjectName("LockBox")
        lock_box.setStyleSheet(
            f"#LockBox {{"
            f" background: {THEME_BG_CARD};"
            f" border: 1px solid {THEME_BORDER};"
            f" border-radius: 14px;"
            f"}}"
        )
        lock_box.setMaximumWidth(480)
        lb_layout = QVBoxLayout(lock_box)
        lb_layout.setContentsMargins(32, 30, 32, 28)
        lb_layout.setSpacing(14)

        lock_icon = QLabel("🔒")
        lock_icon.setAlignment(Qt.AlignCenter)
        f = lock_icon.font(); f.setPointSize(34); lock_icon.setFont(f)
        lock_icon.setStyleSheet(f"color: {THEME_TEXT_MUTED};")
        lb_layout.addWidget(lock_icon)

        lock_title = QLabel("Schreibstil-Auswahl ist gesperrt")
        lock_title.setAlignment(Qt.AlignCenter)
        f = lock_title.font(); f.setPointSizeF(f.pointSizeF() + 4); f.setBold(True); lock_title.setFont(f)
        lock_title.setStyleSheet(f"color: {THEME_TEXT};")
        lb_layout.addWidget(lock_title)

        # Text wird in refresh_lock() je nach Ursache (Ollama fehlt vs.
        # Cleanup deaktiviert) ausgetauscht.
        self._lock_text = QLabel("")
        self._lock_text.setAlignment(Qt.AlignCenter)
        self._lock_text.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
        self._lock_text.setWordWrap(True)
        # Mind. 4 Zeilen reservieren: lock_box wird via Qt.AlignCenter mit
        # seinem sizeHint platziert, das beim Konstruieren mit leerem Text
        # entsteht. Ohne diese Reservierung wird der Hinweistext nach dem
        # späteren setText() abgeschnitten.
        fm = self._lock_text.fontMetrics()
        self._lock_text.setMinimumHeight(fm.lineSpacing() * 4)
        lb_layout.addWidget(self._lock_text)
        lb_layout.addSpacing(6)

        jump_btn = QPushButton("Zu Einstellungen")
        jump_btn.setProperty("role", "primary")
        jump_btn.setMinimumHeight(34)
        jump_btn.setMinimumWidth(180)
        jump_btn.clicked.connect(lambda: self._on_jump_to_settings())
        lb_layout.addWidget(jump_btn, 0, Qt.AlignCenter)

        lock_layout.addWidget(lock_box, 0, Qt.AlignCenter)
        lock_layout.addStretch(2)
        self._stack.addWidget(lock_page)

        # --- Page 1: Karten + Individuell-Editor ---
        cards_page = QWidget()
        cp = QVBoxLayout(cards_page)
        cp.setContentsMargins(36, 32, 36, 28)
        cp.setSpacing(6)

        title = QLabel("Schreibstil")
        title.setProperty("role", "h1")
        cp.addWidget(title)

        sub = QLabel("Wie aufgenommene Sprache automatisch bereinigt wird.")
        sub.setProperty("role", "sub")
        cp.addWidget(sub)
        cp.addSpacing(18)

        # Style-Cards in einem responsiven Grid: 4 Spalten ab >=1100 px,
        # 2 Spalten ab >=800 px, 1 Spalte darunter. Cards strecken sich
        # auf gleiche Breite. Reflow passiert automatisch bei resizeEvent.
        style_grid = _ResponsiveCardGrid(
            breakpoints=((1100, 4), (800, 2), (0, 1)), gap=14,
        )
        for key, label in (
            ("formal",      "Förmlich"),
            ("locker",      "Locker"),
            ("sehr_locker", "Sehr locker"),
            ("custom",      "Individuell"),
        ):
            card = StyleCard(
                key, label,
                STYLE_SAMPLE_INPUT,
                STYLE_SAMPLE_OUTPUTS.get(key, ""),
            )
            card.clicked.connect(self._select)
            style_grid.add_card(card)
            self._cards[key] = card
        cp.addWidget(style_grid)

        cp.addSpacing(16)

        # Individuell-Editor (sichtbar wenn style==custom).
        # WICHTIG zur Layout-Stabilität: cb_layout.setSpacing(12) sorgt
        # dafür, dass Save-Button-Row IMMER 16px Abstand zum Textfeld
        # hält (Spacing + addSpacing(4)) — sonst wandert der Button bei
        # zu schmalem Fenster optisch über das Textfeld.
        self._custom_box = QGroupBox("Individuell-Konfiguration")
        cb_layout = QVBoxLayout(self._custom_box)
        cb_layout.setSpacing(12)
        cb_layout.setContentsMargins(4, 8, 4, 4)

        self._checkbox_widgets = {}
        for key, label in CUSTOM_OPTIONS:
            cb = QCheckBox(label)
            cb.toggled.connect(self._save_custom)
            cb_layout.addWidget(cb)
            self._checkbox_widgets[key] = cb

        cb_layout.addSpacing(10)
        custom_label = QLabel("Eigene Anweisungen (werden an die obigen Regeln angehängt):")
        custom_label.setProperty("role", "muted")
        cb_layout.addWidget(custom_label)
        self._extra_edit = QPlainTextEdit()
        self._extra_edit.setPlaceholderText(CUSTOM_PROMPT_EXAMPLE)
        self._extra_edit.setMinimumHeight(120)
        # Textfeld nimmt die volle Breite des Containers ein und kann
        # vertikal mitwachsen falls Platz da ist.
        self._extra_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        cb_layout.addWidget(self._extra_edit)
        # 16 px Sicherheits-Spacing zwischen Textfeld und Save-Row,
        # zusätzlich zum cb_layout.setSpacing(12).
        cb_layout.addSpacing(4)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self._save_extra_btn = QPushButton("Eigene Anweisungen speichern")
        self._save_extra_btn.setProperty("role", "primary")
        self._save_extra_btn.clicked.connect(self._save_custom)
        save_row.addWidget(self._save_extra_btn)
        cb_layout.addLayout(save_row)

        cp.addWidget(self._custom_box)
        cp.addStretch(1)

        self._stack.addWidget(cards_page)

        self._load_from_config()
        self.refresh_lock()

    def _load_from_config(self):
        style = self.app.config.get("style", "locker")
        for k, card in self._cards.items():
            card.set_selected(k == style)
        cust = self.app.config.get("style_custom", {}) or {}
        cb_state = cust.get("checkboxes", {}) or {}
        for k, w in self._checkbox_widgets.items():
            w.blockSignals(True)
            w.setChecked(bool(cb_state.get(k, False)))
            w.blockSignals(False)
        extra = cust.get("extra_prompt", "") or ""
        self._extra_edit.blockSignals(True)
        self._extra_edit.setPlainText(extra)
        self._extra_edit.blockSignals(False)
        self._custom_box.setVisible(style == "custom")

    def _select(self, key):
        for k, card in self._cards.items():
            card.set_selected(k == key)
        self.app.config["style"] = key
        self._custom_box.setVisible(key == "custom")
        save_config(self.app.config)
        log.info(f"StyleView: style geändert auf '{key}'")

    def _save_custom(self, *_args):
        cust = self.app.config.get("style_custom") or {}
        cust["checkboxes"] = {
            k: bool(w.isChecked()) for k, w in self._checkbox_widgets.items()
        }
        cust["extra_prompt"] = self._extra_edit.toPlainText().strip()
        self.app.config["style_custom"] = cust
        save_config(self.app.config)

    def refresh_lock(self):
        """Page 0 vs Page 1 umschalten. Voraussetzung für die Style-Auswahl
        sind ZWEI Bedingungen: Ollama läuft UND der User hat die KI-
        Bereinigung aktiviert. Sonst hat das Ändern des Stils keinen
        Effekt - dann lieber transparent sperren."""
        ready = self.app.ollama_mgr.is_ready()
        cleanup_on = bool(self.app.cleanup_enabled)
        unlocked = ready and cleanup_on
        if not ready:
            self._lock_text.setText(
                "Ollama ist nicht aktiv. Aktiviere die KI-Textbereinigung "
                "in den Einstellungen, dann schaltet sich der Schreibstil "
                "automatisch frei."
            )
        else:
            self._lock_text.setText(
                "KI-Textbereinigung ist deaktiviert. Aktiviere die Checkbox "
                "in den Einstellungen, um den Schreibstil zu wählen."
            )
        self._stack.setCurrentIndex(1 if unlocked else 0)


# =====================================================================
#  Wörterbuch-View: Eigennamen-Liste + Editor-Dialog.
#  Speichert/lädt über DictionaryStore, Replacement passiert im
#  Audio-Thread direkt nach Whisper.
# =====================================================================

class DictionaryEditDialog(QDialog):
    """Add/Edit-Dialog. Validiert beim OK: korrekte Schreibweise nicht
    leer, mind. 1 Variante, Variante != korrekte Schreibweise. Liefert
    {"correct": str, "variants": list[str]} via .result_data()."""

    def __init__(self, parent=None, *, correct="", variants=None, edit_mode=False):
        super().__init__(parent)
        self.setWindowTitle("Eintrag bearbeiten" if edit_mode else "Neuer Wörterbuch-Eintrag")
        self.setModal(True)
        self.setMinimumWidth(440)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 18)
        v.setSpacing(12)

        head = QLabel("Korrekte Schreibweise")
        head.setStyleSheet(f"color: {THEME_TEXT_SECONDARY}; font-size: 12px;")
        v.addWidget(head)

        self._correct_edit = QLineEdit()
        self._correct_edit.setPlaceholderText("z.B. IQspeakr")
        self._correct_edit.setText(correct or "")
        v.addWidget(self._correct_edit)

        v.addSpacing(4)
        var_lbl = QLabel("Varianten (eine pro Zeile)")
        var_lbl.setStyleSheet(f"color: {THEME_TEXT_SECONDARY}; font-size: 12px;")
        v.addWidget(var_lbl)

        hint = QLabel(
            "Schreibweisen, die Whisper liefert und automatisch durch die "
            "korrekte Schreibung ersetzt werden sollen."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 11px;")
        v.addWidget(hint)

        self._variants_edit = QPlainTextEdit()
        self._variants_edit.setPlaceholderText("ich-sprecher\nix speaker\nEichspeaker")
        self._variants_edit.setMinimumHeight(120)
        if variants:
            self._variants_edit.setPlainText("\n".join(variants))
        v.addWidget(self._variants_edit)

        # Inline-Fehlerzeile, wird bei Validierungsfehler eingeblendet.
        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet(
            f"color: {THEME_DANGER}; font-size: 12px;"
        )
        self._error_lbl.setVisible(False)
        v.addWidget(self._error_lbl)

        v.addSpacing(4)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("Abbrechen")
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)
        self._ok_btn = QPushButton("Speichern")
        self._ok_btn.setProperty("role", "primary")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(self._ok_btn)
        v.addLayout(btn_row)

        self._correct_edit.setFocus()

    def _show_error(self, msg):
        self._error_lbl.setText(msg)
        self._error_lbl.setVisible(True)

    def _on_ok(self):
        correct = self._correct_edit.text().strip()
        if not correct:
            self._show_error("Korrekte Schreibweise darf nicht leer sein.")
            self._correct_edit.setFocus()
            return
        # Varianten: Zeilen splitten, leer rausfiltern, dedupe (case-insensitive).
        raw = self._variants_edit.toPlainText().splitlines()
        variants = []
        seen = set()
        for line in raw:
            v = line.strip()
            if not v or v.lower() in seen:
                continue
            if v.lower() == correct.lower():
                # Eine Variante darf nicht identisch zur korrekten
                # Schreibweise sein - das wäre ein No-Op.
                continue
            seen.add(v.lower())
            variants.append(v)
        if not variants:
            self._show_error(
                "Mindestens eine Variante eintragen — und sie muss sich von "
                "der korrekten Schreibweise unterscheiden."
            )
            self._variants_edit.setFocus()
            return
        self._result = {"correct": correct, "variants": variants}
        self.accept()

    def result_data(self):
        return getattr(self, "_result", None)


class _DictEntryCard(QFrame):
    """Eine Card für einen Wörterbuch-Eintrag in der Liste. Zeigt korrekte
    Schreibweise + Varianten-Pillen und reicht Edit/Delete an die View."""

    edit_requested = Signal(int)
    delete_requested = Signal(int)

    CARD_QSS = (
        f"#DictCard {{"
        f" background: {THEME_BG_CARD};"
        f" border: 1px solid {THEME_BORDER};"
        f" border-radius: 12px;"
        f"}}"
        f"#DictCard:hover {{"
        f" border-color: {THEME_BORDER_HOVER};"
        f"}}"
    )

    PILL_QSS = (
        f"QLabel#VariantPill {{"
        f" background: {THEME_BG_INPUT};"
        f" color: {THEME_TEXT_SECONDARY};"
        f" border: 1px solid {THEME_BORDER};"
        f" border-radius: 10px;"
        f" padding: 3px 9px;"
        f" font-size: 11px;"
        f"}}"
    )

    def __init__(self, idx, correct, variants, parent=None):
        super().__init__(parent)
        self._idx = idx
        self.setObjectName("DictCard")
        self.setStyleSheet(self.CARD_QSS + self.PILL_QSS)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(18, 14, 12, 14)
        outer.setSpacing(14)

        # Linke Spalte: Korrekte Schreibung + Varianten-Pillen.
        left = QVBoxLayout()
        left.setSpacing(8)

        correct_lbl = QLabel(correct)
        f = correct_lbl.font(); f.setPointSizeF(f.pointSizeF() + 2); f.setBold(True)
        correct_lbl.setFont(f)
        correct_lbl.setStyleSheet(f"color: {THEME_TEXT};")
        left.addWidget(correct_lbl)

        # Pillen für Varianten.
        pill_wrap = QWidget()
        pw_layout = QHBoxLayout(pill_wrap)
        pw_layout.setContentsMargins(0, 0, 0, 0)
        pw_layout.setSpacing(6)
        for v in variants:
            pill = QLabel(v)
            pill.setObjectName("VariantPill")
            pw_layout.addWidget(pill)
        pw_layout.addStretch(1)
        left.addWidget(pill_wrap)
        outer.addLayout(left, 1)

        # Rechte Spalte: Edit + Delete als Icon-Buttons.
        edit_btn = QPushButton()
        edit_btn.setIcon(_lucide_icon(_LUCIDE_PENCIL, 16, THEME_TEXT_SECONDARY))
        edit_btn.setFixedSize(32, 32)
        edit_btn.setToolTip("Bearbeiten")
        edit_btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid transparent; border-radius: 6px; }"
            f"QPushButton:hover {{ background: {THEME_BG_HOVER}; border-color: {THEME_BORDER}; }}"
        )
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self._idx))
        outer.addWidget(edit_btn, 0, Qt.AlignTop)

        del_btn = QPushButton()
        del_btn.setIcon(_lucide_icon(_LUCIDE_TRASH, 16, THEME_TEXT_MUTED))
        del_btn.setFixedSize(32, 32)
        del_btn.setToolTip("Löschen")
        del_btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid transparent; border-radius: 6px; }"
            f"QPushButton:hover {{ background: rgba(239, 68, 68, 0.10); border-color: {THEME_DANGER}; }}"
        )
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self._idx))
        outer.addWidget(del_btn, 0, Qt.AlignTop)


class DictionaryView(QWidget):
    """Liste aller Wörterbuch-Einträge + 'Neuer Eintrag'-Button. Reagiert
    auf DictionaryStore.changed() und baut die Card-Liste neu auf."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(36, 32, 36, 28)
        v.setSpacing(16)

        title = QLabel("Wörterbuch")
        title.setProperty("role", "h1")
        v.addWidget(title)

        sub = QLabel(
            "Korrigiere Eigennamen, die Whisper häufig falsch versteht. "
            "Wird vor jeder weiteren Bearbeitung angewendet."
        )
        sub.setProperty("role", "sub")
        sub.setWordWrap(True)
        v.addWidget(sub)
        v.addSpacing(8)

        # Add-Button-Reihe (oberhalb der Liste).
        action_row = QHBoxLayout()
        self._add_btn = QPushButton("  Eintrag hinzufügen")
        self._add_btn.setIcon(_lucide_icon(_LUCIDE_PLUS, 16, "#FFFFFF"))
        self._add_btn.setProperty("role", "primary")
        self._add_btn.setMinimumHeight(34)
        self._add_btn.clicked.connect(self._on_add_clicked)
        action_row.addWidget(self._add_btn)
        action_row.addStretch(1)
        v.addLayout(action_row)

        v.addSpacing(4)

        # Stack: Empty-State <-> Card-Liste.
        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)

        # Empty-State.
        empty_page = QWidget()
        ep = QVBoxLayout(empty_page)
        ep.setContentsMargins(0, 0, 0, 0)
        ep.addStretch(1)
        empty_lbl = QLabel(
            "Noch keine Einträge.\nLege deinen ersten Eigennamen an, "
            "damit Whisper-Fehlschreibungen automatisch korrigiert werden."
        )
        empty_lbl.setAlignment(Qt.AlignCenter)
        empty_lbl.setWordWrap(True)
        empty_lbl.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 13px;")
        ep.addWidget(empty_lbl)
        ep.addStretch(2)
        self._stack.addWidget(empty_page)

        # List-Page: vertikal gestapelte Cards.
        list_page = QWidget()
        lp = QVBoxLayout(list_page)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(10)
        self._list_layout = lp
        lp.addStretch(1)
        self._stack.addWidget(list_page)

        self._refresh()
        self.app.dictionary.changed.connect(self._refresh)

    def _clear_list(self):
        # Alle Cards entfernen, Stretch am Ende beibehalten.
        layout = self._list_layout
        while layout.count() > 1:
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _refresh(self):
        self._clear_list()
        entries = self.app.dictionary.entries()
        if not entries:
            self._stack.setCurrentIndex(0)
            return
        self._stack.setCurrentIndex(1)
        for i, e in enumerate(entries):
            card = _DictEntryCard(i, e["correct"], e["variants"])
            card.edit_requested.connect(self._on_edit)
            card.delete_requested.connect(self._on_delete)
            # Vor dem Stretch (letzter Item) einfügen.
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)

    def _on_add_clicked(self):
        dlg = DictionaryEditDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.result_data()
        if not data:
            return
        # Duplicate-Check: existiert ein Eintrag mit gleicher korrekter
        # Schreibweise schon? Falls ja: nicht doppelt anlegen, sondern
        # User fragen ob die neuen Varianten an den Bestand angehängt
        # werden sollen.
        existing_idx = self.app.dictionary.find_by_correct(data["correct"])
        if existing_idx >= 0:
            box = QMessageBox(self)
            box.setWindowTitle("Eintrag existiert bereits")
            box.setIcon(QMessageBox.Question)
            box.setText(
                f"Für „{data['correct']}“ gibt es bereits einen Eintrag. "
                "Möchtest du die neuen Varianten an den bestehenden Eintrag "
                "anhängen?"
            )
            yes = box.addButton("Anhängen", QMessageBox.AcceptRole)
            box.addButton("Abbrechen", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is yes:
                self.app.dictionary.merge_variants(existing_idx, data["variants"])
            return
        self.app.dictionary.add(data["correct"], data["variants"])

    def _on_edit(self, idx):
        entries = self.app.dictionary.entries()
        if not (0 <= idx < len(entries)):
            return
        e = entries[idx]
        dlg = DictionaryEditDialog(
            self, correct=e["correct"], variants=e["variants"], edit_mode=True,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.result_data()
        if not data:
            return
        # Bei Änderung der korrekten Schreibweise auf einen anderen
        # bestehenden Eintrag: Konflikt anzeigen, nicht überschreiben.
        conflict = self.app.dictionary.find_by_correct(data["correct"])
        if conflict >= 0 and conflict != idx:
            QMessageBox.warning(
                self, "Konflikt",
                f"„{data['correct']}“ ist bereits in einem anderen "
                "Eintrag belegt. Lösche oder bearbeite zuerst den anderen "
                "Eintrag.",
            )
            return
        self.app.dictionary.update(idx, data["correct"], data["variants"])

    def _on_delete(self, idx):
        entries = self.app.dictionary.entries()
        if not (0 <= idx < len(entries)):
            return
        correct = entries[idx]["correct"]
        ans = QMessageBox.question(
            self, "Eintrag löschen",
            f"Eintrag „{correct}“ wirklich löschen?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ans == QMessageBox.Yes:
            self.app.dictionary.remove(idx)


# =====================================================================
#  Settings-View: Hotkey, Whisper, Sprache + Ollama-State-Maschine.
# =====================================================================

class SettingsView(QWidget):
    FORM_QSS = (
        # FormLayout-Labels weicher als der Default-Body-Text:
        "QFormLayout > QLabel, QLabel[role=\"form-label\"] {"
        f" color: {THEME_TEXT_SECONDARY};"
        "}"
    )

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(36, 32, 36, 28)
        v.setSpacing(24)

        title = QLabel("Einstellungen")
        title.setProperty("role", "h1")
        v.addWidget(title)

        sub = QLabel("Hotkey, Spracherkennung und KI-Textbereinigung.")
        sub.setProperty("role", "sub")
        v.addWidget(sub)
        v.addSpacing(8)

        # --- Allgemein ---
        general = QGroupBox("Allgemein")
        gl = QFormLayout(general)
        gl.setHorizontalSpacing(24)
        gl.setVerticalSpacing(16)
        gl.setContentsMargins(0, 4, 0, 0)
        gl.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._hotkey_label = QLabel(hotkey_display(self.app.config.get("hotkey", "")))
        self._hotkey_label.setStyleSheet(f"color: {THEME_TEXT}; font-weight: 500;")
        hot_btn = QPushButton("Ändern...")
        hot_btn.clicked.connect(self._change_hotkey)
        hot_row = QHBoxLayout()
        hot_row.setSpacing(10)
        hot_row.addWidget(self._hotkey_label, 1)
        hot_row.addWidget(hot_btn)
        gl.addRow(self._form_label("Tastenkombination"), self._wrap_row(hot_row))

        self._whisper_combo = QComboBox()
        for size, label in (
            ("tiny",   "tiny - Sehr schnell, ungenau (~75 MB)"),
            ("base",   "base - Guter Kompromiss (~145 MB)"),
            ("small",  "small - Gute Qualität (~465 MB)"),
            ("medium", "medium - Beste Qualität (~1.5 GB)"),
        ):
            self._whisper_combo.addItem(label, size)
        cur_w = self.app.config.get("whisper_model", "base")
        for i in range(self._whisper_combo.count()):
            if self._whisper_combo.itemData(i) == cur_w:
                self._whisper_combo.setCurrentIndex(i)
                break
        self._whisper_combo.currentIndexChanged.connect(self._on_whisper_changed)
        gl.addRow(self._form_label("Whisper-Modell"), self._whisper_combo)

        self._lang_combo = QComboBox()
        for code, label in (
            ("de", "Deutsch"),
            ("en", "Englisch"),
            ("auto", "Automatisch erkennen"),
        ):
            self._lang_combo.addItem(label, code)
        cur_l = self.app.config.get("language", "de")
        for i in range(self._lang_combo.count()):
            if self._lang_combo.itemData(i) == cur_l:
                self._lang_combo.setCurrentIndex(i)
                break
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        gl.addRow(self._form_label("Sprache"), self._lang_combo)

        self._overlay_cb = QCheckBox("Pill-Overlay während Aufnahme anzeigen")
        self._overlay_cb.setChecked(bool(self.app.config.get("overlay_enabled", True)))
        self._overlay_cb.toggled.connect(self._on_overlay_toggled)
        gl.addRow(self._form_label(""), self._overlay_cb)

        self._notify_cb = QCheckBox("Statusmeldungen als Tray-Benachrichtigung anzeigen")
        self._notify_cb.setToolTip(
            "Aus = nur Fehler werden gezeigt. Info-Meldungen wie\n"
            "\"Kein Text erkannt\" oder \"Modell geladen\" werden unterdrückt."
        )
        self._notify_cb.setChecked(bool(self.app.config.get("notify_enabled", True)))
        self._notify_cb.toggled.connect(self._on_notify_toggled)
        gl.addRow(self._form_label(""), self._notify_cb)

        v.addWidget(general)

        # --- Ollama-Block ---
        self._ollama_box = QGroupBox("KI-Textbereinigung (Ollama)")
        ob = QVBoxLayout(self._ollama_box)
        ob.setSpacing(16)
        ob.setContentsMargins(0, 4, 0, 0)

        self._ollama_status = QLabel()
        self._ollama_status.setWordWrap(True)
        self._ollama_status.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
        ob.addWidget(self._ollama_status)

        # Modell-Dropdown: speichert nur die Wahl (kein Auto-Pull mehr).
        # Daneben "Modell herunterladen"-Button bzw. "Bereit"-Label, je nach
        # ob das gewählte Modell schon lokal liegt.
        self._model_combo = QComboBox()
        for key, label in OLLAMA_MODEL_OPTIONS:
            self._model_combo.addItem(label, key)
        cur_m = self.app.config.get("ollama_model", "llama3.2:1b")
        for i in range(self._model_combo.count()):
            if self._model_combo.itemData(i) == cur_m:
                self._model_combo.setCurrentIndex(i)
                break
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)

        self._model_status_lbl = QLabel("")
        self._model_status_lbl.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 11px;")
        self._model_pull_btn = QPushButton("Modell herunterladen")
        self._model_pull_btn.setMinimumHeight(28)
        self._model_pull_btn.setProperty("role", "primary")
        self._model_pull_btn.clicked.connect(self._on_pull_clicked)
        self._model_pull_btn.setVisible(False)

        model_form = QFormLayout()
        model_form.setHorizontalSpacing(20)
        model_form.setVerticalSpacing(8)
        model_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        model_form.addRow(self._form_label("Modell"), self._model_combo)

        # Eigene Zeile für Modell-Status + Download-Button
        model_status_row = QHBoxLayout()
        model_status_row.setSpacing(10)
        model_status_row.addWidget(self._model_status_lbl, 1)
        model_status_row.addWidget(self._model_pull_btn, 0)
        model_form.addRow(self._form_label(""), self._wrap_row(model_status_row))
        ob.addLayout(model_form)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        ob.addWidget(self._progress)

        self._progress_text = QLabel("")
        self._progress_text.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 11px;")
        self._progress_text.setVisible(False)
        ob.addWidget(self._progress_text)

        action_row = QHBoxLayout()
        self._action_btn = QPushButton()
        self._action_btn.setMinimumHeight(34)
        self._action_btn.setMinimumWidth(220)
        self._action_btn.clicked.connect(self._on_action_clicked)
        action_row.addWidget(self._action_btn)
        action_row.addStretch(1)
        ob.addLayout(action_row)

        self._cleanup_cb = QCheckBox("Aufnahmen automatisch bereinigen")
        self._cleanup_cb.setChecked(bool(self.app.config.get("cleanup_enabled", True)))
        self._cleanup_cb.toggled.connect(self._on_cleanup_toggled)
        ob.addWidget(self._cleanup_cb)

        # User-Toggle "Backend aktiv" - getrennt von cleanup_enabled.
        # Wenn aus: ollama serve wird gekillt, kein RAM-Verbrauch, Cleanup
        # übersprungen.
        self._active_cb = QCheckBox("Ollama-Backend aktiv lassen")
        self._active_cb.setToolTip(
            "Aus = ollama serve wird beendet, kein Cleanup, kein RAM-Verbrauch.\n"
            "An = Backend läuft im Hintergrund, sobald IQspeakr offen ist."
        )
        self._active_cb.setChecked(bool(self.app.config.get("ollama_active", True)))
        self._active_cb.toggled.connect(self._on_active_toggled)
        ob.addWidget(self._active_cb)

        # Erklär-Box: wann Ollama-Cleanup wirklich Sinn macht. Auf User-
        # Wunsch deutlich sichtbar als Akzent-getönter Frame mit Info-Icon.
        info_box = QFrame()
        info_box.setObjectName("OllamaInfoBox")
        info_box.setStyleSheet(
            "#OllamaInfoBox {"
            " background: rgba(99, 102, 241, 0.08);"
            " border: 1px solid rgba(99, 102, 241, 0.32);"
            " border-radius: 10px;"
            "}"
        )
        info_outer = QHBoxLayout(info_box)
        info_outer.setContentsMargins(18, 16, 18, 16)
        info_outer.setSpacing(14)

        info_icon = QLabel()
        info_icon.setPixmap(_lucide_icon(_LUCIDE_INFO, 22, THEME_ACCENT).pixmap(22, 22))
        info_icon.setFixedSize(22, 22)
        info_outer.addWidget(info_icon, 0, Qt.AlignTop)

        info_col = QVBoxLayout()
        info_col.setSpacing(10)

        info_title = QLabel("Wann brauchst du KI-Textbereinigung?")
        f = info_title.font()
        f.setPointSizeF(f.pointSizeF() + 1)
        f.setBold(True)
        info_title.setFont(f)
        info_title.setStyleSheet(f"color: {THEME_TEXT};")
        info_col.addWidget(info_title)

        # Body als Rich-Text für ordentlichen Zeilenabstand und visuelle
        # Hierarchie. Die Liste mit Bullet-Symbol ist dem User wichtig.
        info_body = QLabel(
            "<p style='margin: 0 0 8px 0; line-height: 1.5;'>"
            "Whisper setzt bereits automatisch <b>Satzzeichen, Gro&szlig;schreibung</b> "
            "und filtert die meisten <b>F&uuml;llw&ouml;rter</b> (&auml;hm, &auml;h) "
            "sowie Stotterer raus. F&uuml;r klares, ruhiges Diktat reicht das vollkommen."
            "</p>"
            "<p style='margin: 0 0 4px 0;'><b>Cleanup nur einschalten, wenn du:</b></p>"
            "<ul style='margin: 0 0 8px 18px; line-height: 1.5;'>"
            "<li>sehr unkonzentriert sprichst (viele &bdquo;&auml;hm&ldquo;, "
            "&bdquo;halt&ldquo;, &bdquo;also&ldquo;, Wortdoppelungen)</li>"
            "<li>echtes <b>f&ouml;rmliches Schriftdeutsch</b> m&ouml;chtest "
            "(Style &bdquo;F&ouml;rmlich&ldquo;)</li>"
            "<li>eigene Cleanup-Regeln einsetzen willst "
            "(Style &bdquo;Individuell&ldquo;)</li>"
            "</ul>"
            "<p style='margin: 0; line-height: 1.5;'>"
            "<b>Trade-off:</b> Cleanup kostet je nach Modell und Textl&auml;nge "
            "<b>etwa 1&ndash;7 Sekunden</b> pro Aufnahme. Ohne Cleanup landet "
            "der Text fast sofort im Zielfeld."
            "</p>"
        )
        info_body.setWordWrap(True)
        info_body.setTextFormat(Qt.RichText)
        info_body.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
        info_col.addWidget(info_body)

        info_outer.addLayout(info_col, 1)

        ob.addSpacing(6)
        ob.addWidget(info_box)

        v.addWidget(self._ollama_box)

        v.addStretch(1)

        # Signals vom OllamaManager
        self.app.ollama_mgr.state_changed.connect(self._on_state_changed)
        self.app.ollama_mgr.download_progress.connect(self._on_download_progress)
        self.app.ollama_mgr.install_progress.connect(self._on_install_text)
        self.app.ollama_mgr.pull_progress.connect(self._on_pull_progress)
        self.app.ollama_mgr.error_message.connect(self._on_error)

        # Live-Sync von Tray-Submenu zu SettingsView: alle vier Setting-
        # Signale subscriben, damit das UI mitwandert wenn der User
        # über's Tray-Menu was ändert (umgekehrte Richtung lief schon
        # über rebuild_menu_sig).
        self.app.hotkey_changed.connect(self._on_hotkey_changed)
        self.app.whisper_changed.connect(self._on_whisper_remote)
        self.app.language_changed.connect(self._on_language_remote)
        self.app.ollama_model_changed.connect(self._on_ollama_model_remote)

        self._refresh_ollama_ui()

    def _form_label(self, text):
        lbl = QLabel(text)
        lbl.setProperty("role", "form-label")
        lbl.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
        return lbl

    def _wrap_row(self, row_layout):
        w = QWidget()
        w.setLayout(row_layout)
        return w

    def _change_hotkey(self):
        # Konstruktor-Signatur: (parent, initial). Früher waren die
        # Argumente vertauscht -> QDialog bekam einen str als parent
        # und der Klick lief in einen geschluckten TypeError.
        dlg = HotkeyRecorderDialog(self, self.app.config.get("hotkey", ""))
        if dlg.exec() == QDialog.Accepted:
            combo = dlg.result_combo()
            if combo:
                # SoT: Listener neu starten + Label-Sync läuft via
                # _on_hotkey_changed-Slot (am Ende dieser Klasse).
                self.app._apply_hotkey(combo, restart_listener=True)

    def _on_hotkey_changed(self, hotkey_str):
        """Wird von app.hotkey_changed gefeuert. Hier nur das Label
        synchronisieren — die Heavy-Lifting-Arbeit (parser, listener,
        menu-rebuild) macht _apply_hotkey selbst."""
        try:
            self._hotkey_label.setText(hotkey_display(hotkey_str))
        except Exception as e:
            log.warning(f"SettingsView._on_hotkey_changed: {e}")

    def _on_whisper_changed(self, _idx):
        # User hat im Combo etwas angeklickt -> SoT-Apply ruft auch das
        # Live-Update-Signal, das wiederum unser _on_whisper_remote-Slot
        # an den Combo zurückspielt (no-op falls Combo schon da ist).
        size = self._whisper_combo.currentData()
        if size:
            self.app._apply_whisper(size)

    def _on_language_changed(self, _idx):
        code = self._lang_combo.currentData()
        # ComboBox-Daten können None sein (Eintrag "Automatisch") - das
        # ist ein gültiger Wert und KEIN Abbruch-Grund.
        if self._lang_combo.currentIndex() < 0:
            return
        self.app._apply_language(code)

    def _select_combo_data(self, combo, value):
        """Setzt den ComboBox-Index auf den Eintrag mit `value` als itemData,
        ohne currentIndexChanged zu feuern. Verhindert Reentry beim
        Apply-Roundtrip."""
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)
                return

    def _on_whisper_remote(self, size):
        self._select_combo_data(self._whisper_combo, size)

    def _on_language_remote(self, code):
        self._select_combo_data(self._lang_combo, code)

    def _on_ollama_model_remote(self, name):
        self._select_combo_data(self._model_combo, name)

    def _on_overlay_toggled(self, on):
        self.app.config["overlay_enabled"] = bool(on)
        save_config(self.app.config)

    def _on_notify_toggled(self, on):
        self.app.config["notify_enabled"] = bool(on)
        save_config(self.app.config)

    def _on_cleanup_toggled(self, on):
        self.app.config["cleanup_enabled"] = bool(on)
        save_config(self.app.config)
        self.app.cleanup_enabled = bool(on)
        self.app.rebuild_menu_sig.emit()

    def _on_model_changed(self, _idx):
        new_model = self._model_combo.currentData()
        if not new_model:
            return
        # SoT: Apply-Methode kümmert sich um config + refresh_menu + Signal.
        self.app._apply_ollama_model(new_model)
        # KEIN Auto-Pull mehr. _refresh_ollama_ui() zeigt jetzt entweder
        # "Modell bereit" oder den "Modell herunterladen"-Button.
        self._refresh_ollama_ui()
        # Wenn das neue Modell schon lokal liegt: Warmup im Hintergrund,
        # damit der erste Cleanup nicht den Modell-Reload-Tax zahlt.
        if self.app.ollama_mgr.is_ready() and self.app.ollama_mgr.has_model(new_model):
            threading.Thread(target=self.app._ollama_warmup, daemon=True).start()

    def _on_pull_clicked(self):
        new_model = self._model_combo.currentData()
        if not new_model:
            return
        self.app.ollama_mgr.pull_model(new_model)

    def _on_active_toggled(self, on):
        self.app.config["ollama_active"] = bool(on)
        save_config(self.app.config)
        self.app.ollama_mgr.set_user_active(bool(on))

    # --- Ollama-State-Reaktion ---
    def _on_state_changed(self, _state):
        self._refresh_ollama_ui()

    def _on_download_progress(self, done, total):
        if total > 0:
            self._progress.setRange(0, total)
            self._progress.setValue(done)
            mb_done = done / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self._progress_text.setText(f"Download: {mb_done:.1f} / {mb_total:.1f} MB")
        else:
            self._progress.setRange(0, 0)  # indeterminiert
            mb_done = done / (1024 * 1024)
            self._progress_text.setText(f"Download: {mb_done:.1f} MB")
        self._progress.setVisible(True)
        self._progress_text.setVisible(True)

    def _on_install_text(self, text):
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._progress_text.setText(text)
        self._progress_text.setVisible(True)

    def _on_pull_progress(self, percent, status):
        if percent < 0:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(percent)
        self._progress.setVisible(True)
        msg = "Modell-Download"
        if status:
            msg = f"Modell-Download - {status}"
        if percent >= 0:
            msg += f" ({percent}%)"
        self._progress_text.setText(msg)
        self._progress_text.setVisible(True)

    def _on_error(self, msg):
        QMessageBox.warning(self, "Ollama-Fehler", msg)

    def _on_action_clicked(self):
        state = self.app.ollama_mgr.state()
        model = self._model_combo.currentData() or self.app.config.get("ollama_model", "llama3.2")
        if state in (OLLAMA_DOWNLOADING, OLLAMA_INSTALLING, OLLAMA_PULLING):
            # Button ist gerade "Abbrechen" - Cancel an den laufenden Worker.
            self.app.ollama_mgr.cancel()
            return
        if state == OLLAMA_NOT_INSTALLED or state == OLLAMA_ERROR:
            self.app.ollama_mgr.install(model)
        elif state in (OLLAMA_READY, OLLAMA_PAUSED):
            confirm = QMessageBox.question(
                self,
                "Ollama deinstallieren",
                "Ollama wirklich deinstallieren? Heruntergeladene Modelle "
                "bleiben in %USERPROFILE%\\.ollama\\ erhalten.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm == QMessageBox.Yes:
                self.app.ollama_mgr.uninstall()

    def _set_action_role(self, role):
        # Property muss neu gepolisht werden, sonst greifen die globalen
        # Property-Selektoren ([role="primary"] etc.) nicht.
        self._action_btn.setProperty("role", role)
        self._action_btn.style().unpolish(self._action_btn)
        self._action_btn.style().polish(self._action_btn)

    def _refresh_ollama_ui(self):
        state = self.app.ollama_mgr.state()
        # Sichtbarkeit Default zurücksetzen
        self._progress.setVisible(False)
        self._progress_text.setVisible(False)
        # Modell-Status-Label zurücksetzen, wird in den Branches gesetzt
        self._model_pull_btn.setVisible(False)
        self._model_status_lbl.setText("")
        if state == OLLAMA_NOT_INSTALLED:
            self._ollama_status.setText(
                "Ollama ist nicht installiert. Installiere es, um KI-"
                "Textbereinigung zu aktivieren (Download ca. 1.8 GB)."
            )
            self._ollama_status.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
            self._action_btn.setText("Ollama herunterladen und installieren")
            self._set_action_role("primary")
            self._action_btn.setEnabled(True)
            self._action_btn.setVisible(True)
            self._model_combo.setEnabled(True)
            self._cleanup_cb.setEnabled(False)
            self._active_cb.setEnabled(False)
        elif state == OLLAMA_DOWNLOADING:
            self._ollama_status.setText("Lade OllamaSetup.exe herunter...")
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Abbrechen")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._action_btn.setVisible(True)
            self._model_combo.setEnabled(False)
            self._cleanup_cb.setEnabled(False)
            self._active_cb.setEnabled(False)
            self._progress.setVisible(True)
            self._progress_text.setVisible(True)
        elif state == OLLAMA_INSTALLING:
            self._ollama_status.setText("Installer läuft...")
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Abbrechen")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._action_btn.setVisible(True)
            self._model_combo.setEnabled(False)
            self._cleanup_cb.setEnabled(False)
            self._active_cb.setEnabled(False)
            self._progress.setRange(0, 0)
            self._progress.setVisible(True)
            self._progress_text.setVisible(True)
        elif state == OLLAMA_PULLING:
            self._ollama_status.setText("Lade Modell herunter...")
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Abbrechen")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._action_btn.setVisible(True)
            self._model_combo.setEnabled(False)
            self._cleanup_cb.setEnabled(False)
            self._active_cb.setEnabled(False)
            self._progress.setVisible(True)
            self._progress_text.setVisible(True)
        elif state == OLLAMA_READY:
            self._ollama_status.setText("Ollama aktiv. KI-Textbereinigung verfügbar.")
            self._ollama_status.setStyleSheet(f"color: {THEME_SUCCESS}; font-weight: 500;")
            self._action_btn.setText("Ollama deinstallieren")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._action_btn.setVisible(True)
            self._model_combo.setEnabled(True)
            self._cleanup_cb.setEnabled(True)
            self._active_cb.setEnabled(True)
            # Modell-Status-Label + Pull-Button.
            sel_model = self._model_combo.currentData() or self.app.config.get("ollama_model", "")
            if sel_model:
                if self.app.ollama_mgr.has_model(sel_model):
                    self._model_status_lbl.setText("✓ Modell ist bereit")
                    self._model_status_lbl.setStyleSheet(f"color: {THEME_SUCCESS}; font-size: 11px;")
                    self._model_pull_btn.setVisible(False)
                else:
                    self._model_status_lbl.setText("Modell ist noch nicht heruntergeladen.")
                    self._model_status_lbl.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-size: 11px;")
                    self._model_pull_btn.setVisible(True)
        elif state == OLLAMA_PAUSED:
            self._ollama_status.setText(
                "Ollama ist pausiert. Backend läuft nicht, Cleanup übersprungen."
            )
            self._ollama_status.setStyleSheet(f"color: {THEME_TEXT_MUTED}; font-weight: 500;")
            self._action_btn.setText("Ollama deinstallieren")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._action_btn.setVisible(True)
            self._model_combo.setEnabled(True)
            self._cleanup_cb.setEnabled(False)
            self._active_cb.setEnabled(True)
        else:  # OLLAMA_ERROR
            self._ollama_status.setText("Fehler. Versuche es erneut.")
            self._ollama_status.setStyleSheet(f"color: {THEME_DANGER};")
            self._action_btn.setText("Erneut versuchen")
            self._set_action_role("primary")
            self._action_btn.setEnabled(True)
            self._action_btn.setVisible(True)
            self._model_combo.setEnabled(True)
            self._cleanup_cb.setEnabled(False)
            self._active_cb.setEnabled(True)


# =====================================================================
#  Main-Window: Sidebar + QStackedWidget mit drei Views.
# =====================================================================

class MainWindow(QMainWindow):
    # (key, label, lucide-svg). Reihenfolge = UI-Reihenfolge.
    NAV_ITEMS = [
        ("home",       "Home",       _LUCIDE_HOME),
        ("dashboard",  "Dashboard",  _LUCIDE_BAR_CHART),
        ("style",      "Style",      _LUCIDE_TYPE),
        ("dictionary", "Wörterbuch", _LUCIDE_BOOK),
    ]

    # Sidebar-QSS: 11/14-Padding, 6px-Radius, Hover-Tint, Active mit
    # 3px Akzent-Strich links + kräftigerer Schrift. padding-left wird
    # im selected-State um 3px reduziert, damit der Text nicht springt
    # wenn der Strich erscheint.
    SIDEBAR_LIST_QSS = (
        f"QListWidget#SidebarNav {{"
        f" background: transparent; color: {THEME_TEXT_SECONDARY};"
        f" border: none; outline: 0; padding: 8px 8px;"
        f"}}"
        f"QListWidget#SidebarNav::item {{"
        f" padding: 11px 14px; border-radius: 6px; margin: 2px 0;"
        f" border-left: 3px solid transparent;"
        f"}}"
        f"QListWidget#SidebarNav::item:hover {{"
        f" background: {THEME_BG_HOVER}; color: {THEME_TEXT};"
        f"}}"
        f"QListWidget#SidebarNav::item:selected {{"
        f" background: {THEME_ACCENT_SOFT}; color: {THEME_TEXT};"
        f" border-left: 3px solid {THEME_ACCENT};"
        f" padding-left: 11px;"
        f" font-weight: 600;"
        f"}}"
    )

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self._first_show = True
        self.setWindowTitle("IQspeakr")
        self.setMinimumSize(960, 600)
        if os.path.exists(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))

        central = QWidget()
        self.setCentralWidget(central)
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # --- Sidebar ---
        sidebar_wrap = QWidget()
        sidebar_wrap.setObjectName("SidebarWrap")
        sidebar_wrap.setFixedWidth(220)
        sidebar_wrap.setStyleSheet(
            f"#SidebarWrap {{"
            f" background: {THEME_BG_SIDEBAR};"
            f" border-right: 1px solid {THEME_BORDER};"
            f"}}"
        )
        sw_layout = QVBoxLayout(sidebar_wrap)
        # Unten Luft, damit Settings nicht direkt auf der Fensterkante klebt.
        sw_layout.setContentsMargins(0, 0, 0, 20)
        sw_layout.setSpacing(0)

        # --- Sidebar-Header (Logo + App-Name) ---
        header = QWidget()
        header.setFixedHeight(64)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(18, 14, 16, 14)
        hl.setSpacing(10)

        if os.path.exists(APP_ICON_PATH):
            icon_lbl = QLabel()
            pix = QIcon(APP_ICON_PATH).pixmap(28, 28)
            icon_lbl.setPixmap(pix)
            icon_lbl.setFixedSize(28, 28)
            hl.addWidget(icon_lbl)

        name_lbl = QLabel("IQspeakr")
        name_font = name_lbl.font()
        name_font.setPointSizeF(name_font.pointSizeF() + 1.5)
        name_font.setBold(True)
        name_lbl.setFont(name_font)
        name_lbl.setStyleSheet(f"color: {THEME_TEXT};")
        hl.addWidget(name_lbl)
        hl.addStretch(1)

        sw_layout.addWidget(header)

        header_sep = QFrame()
        header_sep.setFixedHeight(1)
        header_sep.setStyleSheet(f"background: {THEME_BORDER};")
        sw_layout.addWidget(header_sep)

        # --- Sidebar-Top-Nav ---
        self._sidebar = QListWidget()
        self._sidebar.setObjectName("SidebarNav")
        self._sidebar.setStyleSheet(self.SIDEBAR_LIST_QSS)
        self._sidebar.setFocusPolicy(Qt.NoFocus)
        self._sidebar.setFrameShape(QFrame.NoFrame)
        self._sidebar.setIconSize(QSize(18, 18))

        for key, label, svg in self.NAV_ITEMS:
            it = QListWidgetItem(_lucide_icon(svg, 18, THEME_TEXT_SECONDARY), "  " + label)
            it.setData(Qt.UserRole, key)
            self._sidebar.addItem(it)
        sw_layout.addWidget(self._sidebar, 1)

        # --- Trenner über Settings (sehr dezent, low-opacity) ---
        bottom_sep = QFrame()
        bottom_sep.setFixedHeight(1)
        bottom_sep.setStyleSheet(f"background: {THEME_BORDER_SOFT};")
        sw_layout.addWidget(bottom_sep)

        self._settings_list = QListWidget()
        self._settings_list.setObjectName("SidebarNav")
        self._settings_list.setStyleSheet(self.SIDEBAR_LIST_QSS)
        # 78px: Item-Render-Höhe ~52, dazu 8px List-Pad-Top + 18px Spielraum
        # nach unten. Gibt dem Active-State-Hintergrund (border-radius 6)
        # vollständig Platz und lässt das Item visuell etwas höher sitzen
        # statt direkt am Fensterrand zu kleben.
        self._settings_list.setFixedHeight(78)
        self._settings_list.setFocusPolicy(Qt.NoFocus)
        self._settings_list.setFrameShape(QFrame.NoFrame)
        self._settings_list.setIconSize(QSize(18, 18))
        si = QListWidgetItem(_lucide_icon(_LUCIDE_SETTINGS, 18, THEME_TEXT_SECONDARY), "  Settings")
        si.setData(Qt.UserRole, "settings")
        self._settings_list.addItem(si)
        sw_layout.addWidget(self._settings_list)

        h.addWidget(sidebar_wrap)

        # --- Content-Bereich ---
        content_wrap = QWidget()
        content_wrap.setObjectName("ContentWrap")
        content_wrap.setStyleSheet(f"#ContentWrap {{ background: {THEME_BG}; }}")
        cwl = QVBoxLayout(content_wrap)
        cwl.setContentsMargins(0, 0, 0, 0)
        cwl.setSpacing(0)

        self._stack = QStackedWidget()
        cwl.addWidget(self._stack)
        h.addWidget(content_wrap, 1)

        self.home_view = HomeView(self.app)
        self.dashboard_view = DashboardView(self.app)
        self.style_view = StyleView(self.app, on_jump_to_settings=self._goto_settings)
        self.dictionary_view = DictionaryView(self.app)
        self.settings_view = SettingsView(self.app)

        self._stack.addWidget(self.home_view)
        self._stack.addWidget(self.dashboard_view)
        self._stack.addWidget(self.style_view)
        self._stack.addWidget(self.dictionary_view)
        self._stack.addWidget(self.settings_view)

        # Mapping nav-key -> stack-index
        self._nav_idx = {
            "home": 0, "dashboard": 1, "style": 2,
            "dictionary": 3, "settings": 4,
        }

        self._sidebar.currentRowChanged.connect(self._on_top_nav_changed)
        self._settings_list.itemClicked.connect(self._on_settings_clicked)
        self._sidebar.setCurrentRow(0)

        # Style-Sperre reagiert auf zwei Signale:
        #  1. Ollama-State-Wechsel
        #  2. cleanup_enabled-Toggle in der SettingsView (läuft über
        #     rebuild_menu_sig, das eh nach jedem Settings-Change feuert).
        self.app.ollama_mgr.state_changed.connect(self._on_ollama_state)
        self.app.rebuild_menu_sig.connect(self._on_settings_changed)

    def _on_top_nav_changed(self, row):
        if 0 <= row < len(self.NAV_ITEMS):
            key = self.NAV_ITEMS[row][0]
            self._stack.setCurrentIndex(self._nav_idx[key])
            self._settings_list.clearSelection()

    def _on_settings_clicked(self, _item):
        self._stack.setCurrentIndex(self._nav_idx["settings"])
        self._sidebar.clearSelection()
        self._sidebar.setCurrentRow(-1)

    def _goto_settings(self):
        self._on_settings_clicked(None)

    def _on_ollama_state(self, _state):
        self.style_view.refresh_lock()

    def _on_settings_changed(self):
        # rebuild_menu_sig feuert nach jedem Settings-Change (z.B.
        # Cleanup-Toggle). Wir lassen die Style-View ihre Lock-Logik
        # neu auswerten, damit Off->On + On->Off sofort wirkt.
        self.style_view.refresh_lock()

    def showEvent(self, event):
        # Default-Geometry beim allerersten Show anwenden, NACHDEM alle
        # Widgets layoutet sind und Qt seine Layout-Hints final hat.
        # Sonst greift resize() nicht zuverlässig (Höhe oft zu klein).
        # Bei nachfolgenden show()-Aufrufen (Tray-Doppelklick nach hide())
        # behält das Fenster die Größe die der User gesetzt hat.
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            self._apply_default_geometry()

    def _apply_default_geometry(self):
        try:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            target_w = min(1180, int(screen.width() * 0.85))
            target_h = min(780, int(screen.height() * 0.85))
            self.resize(max(target_w, 960), max(target_h, 600))
            self.move(
                screen.x() + (screen.width() - self.width()) // 2,
                screen.y() + (screen.height() - self.height()) // 2,
            )
        except Exception as e:
            log.warning(f"MainWindow: Default-Geometry-Fehler: {e}")
            self.resize(1180, 780)

    def closeEvent(self, event):
        # Schließen versteckt nur, App lebt im Tray weiter.
        self.hide()
        event.ignore()


# =====================================================================
#  Haupt-App: QObject mit Signals für thread-safe GUI-Updates.
# =====================================================================

class IQspeakrApp(QObject):

    # Signals, die Worker-Threads emittieren können, um die GUI
    # (tray-icon, menü, notifications) im Main-Thread zu aktualisieren.
    icon_state_sig = Signal(str)
    rebuild_menu_sig = Signal()
    # 3. Argument ist Level: "info" (per User-Toggle ausschaltbar) oder
    # "error" (immer angezeigt, auch bei notify_enabled=False).
    notify_sig = Signal(str, str, str)
    status_sig = Signal(str)
    # Overlay-Show/Hide MUSS über Signal laufen, nicht direkt: _start_recording
    # / _stop_recording werden aus dem pynput-Listener-Thread aufgerufen, und
    # QWidget.show()/hide() greifen nur im Main-Thread.
    overlay_recording_sig = Signal(bool)
    # Wird gefeuert sobald die Tastenkombination geändert wurde
    # (egal ob aus Settings-View oder Tray-Submenu). SettingsView hängt
    # sich ran um ihr Label live zu aktualisieren — vorher gab's eine
    # stale Anzeige, weil das Label nur beim Settings-Init gerendert wurde.
    hotkey_changed = Signal(str)
    # Analoge Signale für die anderen Settings — alle Pfade laufen über
    # _apply_whisper / _apply_language / _apply_ollama_model und feuern
    # diese Signals nach erfolgreichem Schreiben. SettingsView reagiert
    # mit Combo-Selection-Update, Tray rebuildet sein Submenu via
    # rebuild_menu_sig (das aus den apply-Methoden mit ausgelöst wird).
    whisper_changed = Signal(str)
    language_changed = Signal(object)  # str oder None
    ollama_model_changed = Signal(str)

    def __init__(self, qapp, splash=None):
        super().__init__()
        self.qapp = qapp
        # Splash bleibt sichtbar bis status="Bereit". Sicherheits-Timer in
        # main() stellt sicher, dass die Splash spätestens nach 30s verschwindet.
        self._splash = splash
        self.config = load_config()
        self.recording = False
        self.audio_frames = []
        # Persistenter Audio-Stream: einmal geöffnet, lebt bis zum Quit.
        # Vermeidet PortAudio-Races bei rapidem open/close.
        self._persistent_stream = None
        # Sperrt Listener kurzzeitig während wir Ctrl+V simulieren - sonst
        # sieht pynput die simulierten Keys als Hotkey-Press (Self-Trigger).
        self._suppress_listener = False

        # Pill-Overlay (QWidget) - im Main-Thread erzeugt, thread-safe via Signals.
        # Wird NICHT direkt angezeigt - erscheint erst bei set_recording(True).
        self.overlay = PillOverlay(enabled=self.config.get("overlay_enabled", True))

        self._stream_lock = threading.Lock()
        self.model = None
        self.hotkey_matchers = parse_hotkey(self.config["hotkey"])
        self.hotkey_label = hotkey_display(self.config["hotkey"])
        self._modifier_only_mode = _hotkey_is_all_modifiers(self.config["hotkey"])

        self._ctrl_press_time = 0
        self._last_tap_time = 0
        self._continuous_mode = False
        self._hold_mode = False
        self._hold_threshold = 0.3
        self._double_tap_window = 0.4

        self._pressed_keys = set()
        self._combo_active = False

        self._kb_controller = Controller()

        self.cleanup_enabled = self.config["cleanup_enabled"]
        self.ollama_available = False

        # History + Stats + Wörterbuch + Ollama-Manager. main_window wird
        # lazy beim ersten Open instanziiert.
        self.history = HistoryStore(self)
        self.stats = StatsStore(self)
        self.dictionary = DictionaryStore(self)
        # Einmal-Migration: alte history.json-Einträge ins Dashboard,
        # damit es nicht mit Empty-State startet.
        try:
            self.stats.import_legacy_history(self.history.items())
        except Exception as e:
            log.warning(f"Stats-Migration fehlgeschlagen: {e}")
        self.ollama_mgr = OllamaManager(self)
        # User-Toggle aus Config in den Manager spiegeln, BEVOR refresh_state
        # später im _load_model das Backend hochfährt.
        self.ollama_mgr._user_active = bool(self.config.get("ollama_active", True))
        self.ollama_mgr.state_changed.connect(self._on_ollama_state_changed)
        self.main_window = None

        self._status_text = "Modell wird geladen..."

        # Tray-Icon
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(_make_icon_pixmap("ready")))
        self.tray.setToolTip("IQspeakr")
        self._menu = QMenu()
        self._build_menu()
        self.tray.setContextMenu(self._menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        # Signals -> Slots (automatisch queued wenn emitted aus anderem Thread).
        self.icon_state_sig.connect(self._on_icon_state)
        self.rebuild_menu_sig.connect(self._build_menu)
        self.notify_sig.connect(self._on_notify)
        self.status_sig.connect(self._on_status)
        self.overlay_recording_sig.connect(self.overlay.set_recording)

        # Whisper-Modell laden (Thread)
        threading.Thread(target=self._load_model, daemon=True).start()

        # Global Hotkey-Listener (pynput)
        self._listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self._listener.daemon = True
        self._listener.start()
        log.info("pynput Keyboard-Listener aktiv - Hotkey-Erkennung läuft")

    # --- Slots (laufen immer im Main-Thread) ---

    def _on_icon_state(self, state):
        try:
            self.tray.setIcon(QIcon(_make_icon_pixmap(state)))
        except Exception as e:
            log.warning(f"Icon-Update fehlgeschlagen: {e}")

    def _on_notify(self, title, message, level):
        # Info-Notifications nur zeigen wenn der User sie eingeschaltet hat.
        # Error-Notifications IMMER zeigen, sonst sieht der User nicht wenn
        # was Kaputtes passiert (Modell-Lade-Fehler, Whisper-Crash, ...).
        if level != "error" and not self.config.get("notify_enabled", True):
            return
        try:
            self.tray.showMessage(title, message, QSystemTrayIcon.Information, 4000)
        except Exception as e:
            log.warning(f"Notification fehlgeschlagen: {e}")

    def _on_status(self, text):
        self._status_text = text
        self.rebuild_menu_sig.emit()
        if text == "Bereit" and self._splash is not None:
            try:
                self._splash.close()
            except Exception:
                pass
            self._splash = None

    # Thin wrappers, damit Call-Sites wie vorher bleiben können.
    def _set_icon_state(self, state):
        self.icon_state_sig.emit(state)

    def _notify(self, title, message, level="info"):
        self.notify_sig.emit(title, message, level)

    def _set_status(self, text):
        self.status_sig.emit(text)

    def _refresh_menu(self):
        self.rebuild_menu_sig.emit()

    # --- Hauptfenster + Tray-Activation + Ollama-Status ---

    def _show_main_window(self):
        if self.main_window is None:
            self.main_window = MainWindow(self)
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def _on_tray_activated(self, reason):
        # DoubleClick auf Tray-Icon öffnet das Hauptfenster.
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_main_window()

    def _on_ollama_state_changed(self, state):
        # Halte ollama_available im Sync mit dem Manager-State, damit der
        # bestehende Tray-Submenu-Aufbau (der ollama_available abfragt)
        # weiter funktioniert.
        was = self.ollama_available
        self.ollama_available = (state == OLLAMA_READY)
        if state != OLLAMA_READY:
            self.cleanup_enabled = False
        elif not was:
            # Ollama frisch verfügbar -> Cleanup wieder aktivieren falls
            # User es eingeschaltet hatte, plus Warmup-Anfrage damit das
            # Modell schon im RAM ist wenn der erste Hotkey kommt.
            self.cleanup_enabled = bool(self.config.get("cleanup_enabled", True))
            threading.Thread(target=self._ollama_warmup, daemon=True).start()
        self.rebuild_menu_sig.emit()

    def _ollama_warmup(self):
        """Mini-Generation gegen das aktuell konfigurierte Modell, damit
        Ollama es ins RAM lädt + keep_alive setzt. Spart 5-10s beim
        ersten echten Cleanup nach App-Start."""
        try:
            model = self.config.get("ollama_model", "llama3.2:1b")
            # Prüfe ob das Modell überhaupt gepullt ist - sonst sinnlos.
            if not self.ollama_mgr.has_model(model):
                log.info(f"Ollama-Warmup: Modell '{model}' nicht installiert, skip")
                return
            payload = json.dumps({
                "model": model,
                "prompt": "Hi",
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_predict": 1, "temperature": 0},
            }).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_URL, data=payload,
                headers={"Content-Type": "application/json"},
            )
            t0 = _time.time()
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
            log.info(f"Ollama-Warmup ({model}): {(_time.time() - t0) * 1000:.0f}ms")
        except Exception as e:
            log.warning(f"Ollama-Warmup fehlgeschlagen: {e}")

    # --- Hilfsmethoden, von der SettingsView aufgerufen ---

    def _reload_whisper_model(self):
        """Whisper-Modell neu laden (im Hintergrund-Thread). Unterbindet
        Aufnahmen während des Ladens, damit niemand in ein None-Modell hineinruft."""
        old = self.model
        self.model = None
        self._set_status("Modell wird geladen...")

        def worker():
            try:
                size = self.config["whisper_model"]
                log.info(f"Reload Whisper-Modell '{size}' ...")
                self.model = WhisperModel(size, device="cpu", compute_type="int8")
                log.info("Whisper-Modell neu geladen")
                self._set_status("Bereit")
            except Exception:
                log.exception("Reload Whisper-Modell fehlgeschlagen")
                self.model = old
                self._set_status("Fehler beim Modell-Laden")
        threading.Thread(target=worker, daemon=True).start()

    # --- Menü-Bau ---

    def _build_menu(self):
        """QMenu neu aufbauen. Wird bei jedem Config-Change aufgerufen."""
        self._menu.clear()

        # Hauptfenster öffnen - vor allen anderen Einträgen, damit es
        # auf den ersten Blick erreichbar ist.
        act_open = QAction("Hauptfenster öffnen", self._menu)
        act_open.triggered.connect(lambda _=False: self._show_main_window())
        self._menu.addAction(act_open)

        self._menu.addSeparator()

        # Haupt: Aufnahme starten/stoppen
        if not self.recording:
            lbl = f"Aufnahme starten ({self.hotkey_label} halten)"
        else:
            lbl = f"Aufnahme stoppen ({self.hotkey_label} loslassen)"
        act_rec = QAction(lbl, self._menu)
        act_rec.triggered.connect(lambda _=False: self.toggle_recording(None))
        self._menu.addAction(act_rec)

        self._menu.addSeparator()

        # KI-Bereinigung (nur wenn Ollama läuft)
        if self.ollama_available:
            lbl = f"KI-Bereinigung: {'An' if self.cleanup_enabled else 'Aus'}"
            act_cleanup = QAction(lbl, self._menu)
            act_cleanup.triggered.connect(lambda _=False: self.toggle_cleanup(None))
            self._menu.addAction(act_cleanup)

        # Einstellungen-Untermenü
        settings = self._menu.addMenu("Einstellungen")
        self._build_hotkey_submenu(settings.addMenu("Tastenkombination"))
        self._build_whisper_submenu(settings.addMenu("Whisper-Modell (Spracherkennung)"))
        if self.ollama_available:
            self._build_ollama_submenu(settings.addMenu("Ollama-Modell (Textbereinigung)"))
        self._build_lang_submenu(settings.addMenu("Sprache"))

        self._menu.addSeparator()

        # Status (disabled)
        status_act = QAction(f"Status: {self._status_text}", self._menu)
        status_act.setEnabled(False)
        self._menu.addAction(status_act)

        cfg_act = QAction("Konfig-Datei öffnen", self._menu)
        cfg_act.triggered.connect(lambda _=False: self.open_config(None))
        self._menu.addAction(cfg_act)

        self._menu.addSeparator()
        quit_act = QAction("Beenden", self._menu)
        quit_act.triggered.connect(lambda _=False: self._quit())
        self._menu.addAction(quit_act)

    def _build_hotkey_submenu(self, sub):
        hotkey_options = ["ctrl+shift", "ctrl", "shift", "alt", "win"]
        group = QActionGroup(sub)
        group.setExclusive(True)
        current = self.config.get("hotkey", "")
        current_matched = False
        for opt in hotkey_options:
            a = QAction(f"{hotkey_display(opt)} halten", sub)
            a.setCheckable(True)
            if current == opt:
                a.setChecked(True)
                current_matched = True
            a.triggered.connect(self._make_hotkey_callback(opt))
            group.addAction(a)
            sub.addAction(a)
        sub.addSeparator()
        custom = QAction("Eigene Kombination...", sub)
        custom.setCheckable(True)
        # Wenn der aktuelle Hotkey keine Preset-Option ist, zählt er als "custom".
        if not current_matched and current:
            custom.setChecked(True)
            custom.setText(f"Eigene Kombination: {hotkey_display(current)}...")
        custom.triggered.connect(lambda _=False: self._custom_hotkey())
        group.addAction(custom)
        sub.addAction(custom)

    def _build_whisper_submenu(self, sub):
        whisper_options = [
            ("tiny", "tiny - Sehr schnell, ungenauer (~75 MB)"),
            ("base", "base - Guter Kompromiss (~145 MB)"),
            ("small", "small - Gute Qualität (~465 MB)"),
            ("medium", "medium - Empfohlen, beste Qualität (~1.5 GB)"),
        ]
        group = QActionGroup(sub)
        group.setExclusive(True)
        for size, lbl in whisper_options:
            a = QAction(lbl, sub)
            a.setCheckable(True)
            a.setChecked(self.config["whisper_model"] == size)
            a.triggered.connect(self._make_whisper_callback(size))
            group.addAction(a)
            sub.addAction(a)

    def _build_ollama_submenu(self, sub):
        ollama_options = [
            ("llama3.2", "llama3.2 - Klein & schnell (3B)"),
            ("llama3.1", "llama3.1 - Bessere Qualität (8B)"),
            ("mistral", "mistral - Gut für Deutsch/Europäisch (7B)"),
            ("gemma2", "gemma2 - Google, solide Qualität (9B)"),
            ("phi3", "phi3 - Microsoft, kompakt & gut (3.8B)"),
        ]
        group = QActionGroup(sub)
        group.setExclusive(True)
        for m, lbl in ollama_options:
            a = QAction(lbl, sub)
            a.setCheckable(True)
            a.setChecked(self.config["ollama_model"] == m)
            a.triggered.connect(self._make_ollama_callback(m))
            group.addAction(a)
            sub.addAction(a)

    def _build_lang_submenu(self, sub):
        lang_options = [
            (None, "Automatisch"),
            ("de", "Deutsch"),
            ("en", "English"),
            ("fr", "Francais"),
            ("es", "Espanol"),
            ("it", "Italiano"),
        ]
        group = QActionGroup(sub)
        group.setExclusive(True)
        for code, lbl in lang_options:
            a = QAction(lbl, sub)
            a.setCheckable(True)
            a.setChecked(self.config.get("language") == code)
            a.triggered.connect(self._make_lang_callback(code))
            group.addAction(a)
            sub.addAction(a)

    # --- Callback-Factories ---

    def _make_hotkey_callback(self, hotkey_str):
        def cb(_checked=False):
            self._apply_hotkey(hotkey_str)
        return cb

    def _apply_hotkey(self, hotkey_str, restart_listener=False):
        """Single Source of Truth für Hotkey-Änderungen. Egal aus welcher
        UI-Stelle der Wert kommt (Tray-Submenu-Preset, Tray-Custom-Dialog,
        Settings-Dialog) — alle Pfade müssen hier durch.

        Macht in Reihenfolge:
          1. config schreiben + persistieren
          2. parser-State neu setzen (matchers, label, modifier_only)
          3. State-Machine-Variablen reset (sonst hängt ein alter Hold)
          4. optional Listener neu starten (default nicht, der laufende
             Listener liest self.hotkey_matchers live)
          5. rebuild_menu_sig feuern -> Tray syncht den neuen Wert
          6. hotkey_changed feuern -> SettingsView aktualisiert ihr Label
          7. Toast-Notification (nur bei echter Änderung — sonst nervt
             ein Re-Apply via Tray-Submenu mit identischem Wert)
        """
        previous = self.config.get("hotkey")
        log.info(f"_apply_hotkey: new={hotkey_str!r} previous={previous!r} restart_listener={restart_listener}")
        self.config["hotkey"] = hotkey_str
        self.hotkey_matchers = parse_hotkey(hotkey_str)
        self.hotkey_label = hotkey_display(hotkey_str)
        self._modifier_only_mode = _hotkey_is_all_modifiers(hotkey_str)
        self._pressed_keys.clear()
        self._combo_active = False
        self._hold_mode = False
        self._continuous_mode = False
        save_config(self.config)
        if restart_listener:
            try:
                if self._listener is not None:
                    self._listener.stop()
            except Exception:
                pass
            self._listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            self._listener.daemon = True
            self._listener.start()
        self._refresh_menu()
        # Subscriber benachrichtigen (SettingsView hört hier mit).
        self.hotkey_changed.emit(hotkey_str)
        if previous != hotkey_str:
            self._notify("IQspeakr", f"Neuer Hotkey: {self.hotkey_label} halten")

    def _custom_hotkey(self):
        dlg = HotkeyRecorderDialog(initial=self.config.get("hotkey", ""))
        if dlg.exec() != QDialog.Accepted:
            return
        hotkey_str = dlg.result_combo()
        if not hotkey_str:
            return
        matchers = parse_hotkey(hotkey_str)
        if matchers:
            self._apply_hotkey(hotkey_str)
        else:
            self._notify("IQspeakr", f"Ungültige Kombination: {hotkey_str}")

    def _make_whisper_callback(self, size):
        def cb(_checked=False):
            self._apply_whisper(size)
        return cb

    def _apply_whisper(self, size):
        """SoT für Whisper-Modell-Wechsel. Tray-Submenu UND
        SettingsView.whisper_combo laufen hier durch."""
        if size == self.config.get("whisper_model"):
            return
        if not _whisper_model_cached(size):
            mb = {"tiny": 75, "base": 145, "small": 465, "medium": 1500}.get(size, 0)
            reply = QMessageBox.question(
                None,
                f"Whisper-Modell '{size}' herunterladen?",
                f"Das Modell ist noch nicht auf deinem PC.\n"
                f"Es werden ca. {mb} MB heruntergeladen.",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if reply != QMessageBox.Ok:
                return
        self.config["whisper_model"] = size
        save_config(self.config)
        self.model = None
        self._set_status(f"Lade Whisper '{size}'...")
        self._refresh_menu()
        self.whisper_changed.emit(size)
        threading.Thread(target=self._load_model, daemon=True).start()

    def _make_ollama_callback(self, model_name):
        def cb(_checked=False):
            self._apply_ollama_model(model_name)
        return cb

    def _apply_ollama_model(self, model_name):
        """SoT für Ollama-Modell-Wechsel. KEIN Auto-Pull — der User
        triggert den Download bewusst per Button in der Settings-View."""
        if model_name == self.config.get("ollama_model"):
            return
        self.config["ollama_model"] = model_name
        save_config(self.config)
        self._refresh_menu()
        self.ollama_model_changed.emit(model_name)
        self._notify("IQspeakr", f"Ollama-Modell geändert: {model_name}")

    def _make_lang_callback(self, lang_code):
        def cb(_checked=False):
            self._apply_language(lang_code)
        return cb

    def _apply_language(self, lang_code):
        """SoT für Sprach-Wechsel."""
        if lang_code == self.config.get("language"):
            return
        self.config["language"] = lang_code
        save_config(self.config)
        self._refresh_menu()
        self.language_changed.emit(lang_code)
        lbl = "Automatisch" if lang_code is None else lang_code
        self._notify("IQspeakr", f"Sprache geändert: {lbl}")

    def open_config(self, _sender):
        try:
            os.startfile(CONFIG_PATH)
        except Exception as e:
            log.warning(f"open_config: {e}")

    def _quit(self):
        try:
            self._listener.stop()
        except Exception:
            pass
        try:
            if self._persistent_stream is not None:
                self._persistent_stream.stop()
                self._persistent_stream.close()
        except Exception:
            pass
        self.tray.hide()
        self.qapp.quit()

    # --- Modell laden ---

    def _load_model(self):
        try:
            size = self.config["whisper_model"]
            log.info(f"Lade Whisper-Modell '{size}' (faster-whisper, int8 CPU)...")
            self.model = WhisperModel(size, device="cpu", compute_type="int8")
            log.info("Whisper-Modell erfolgreich geladen")
        except Exception:
            log.exception("FATAL: WhisperModel-Laden ist gescheitert")
            self._set_status("Fehler beim Modell-Laden")
            self._notify("IQspeakr - Fehler", "Modell-Laden gescheitert. Siehe IQspeakr.log.", level="error")
            return

        # Persistenten Audio-Stream öffnen - bleibt bis zum Quit aktiv.
        try:
            self._persistent_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self._audio_callback,
            )
            self._persistent_stream.start()
            log.info("Persistenter Audio-Stream gestartet")
        except Exception as e:
            log.error(f"Audio-Stream-Init-Fehler: {e}")
        # Ollama-Status asynchron prüfen (kein Blocken des Modell-Lade-
        # Threads). _on_ollama_state_changed setzt self.ollama_available.
        self.ollama_mgr.refresh_state(self.config.get("ollama_model"))
        self._set_status("Bereit")
        self._notify(
            "IQspeakr - Modell geladen",
            f"Whisper '{self.config['whisper_model']}' ist bereit.\n"
            f"{self.hotkey_label} gedrückt halten = Aufnahme\n"
            f"{self.hotkey_label} 2x tippen = Daueraufnahme",
        )

    def _cleanup_text(self, text):
        """Cleanup via Ollama, falls aktiviert + Service erreichbar.
        Prompt wird aus dem aktuell gewählten Style gebaut.

        Speed-Optimierungen:
        - Bypass bei <=3 Wörtern ohne Satzzeichen (Mini-Aufnahmen wie "Ja",
          "Test", "Okay") - spart ~2-4s
        - keep_alive=30m: Modell bleibt im RAM zwischen Aufrufen
        - temperature=0 + top_k=1: Greedy-Decoding, deterministisch + schneller
        - num_predict ~2.5x Wortanzahl: Modell stoppt früher
        - num_thread=8: nutzt mehr CPU-Threads (default ist konservativ)
        """
        if not self.cleanup_enabled or not self.ollama_mgr.is_ready():
            return text
        word_count = max(1, len(text.split()))
        # Speed-Bypass für Mini-Aufnahmen.
        if word_count <= 3 and not any(c in text for c in ".,!?;:"):
            log.info(f"Cleanup-Bypass: {word_count} Wörter ohne Satzzeichen")
            return text
        try:
            prompt_template = get_cleanup_prompt(self.config)
            # Eigennamen aus dem Wörterbuch in den Prompt einbetten, damit
            # Ollama die nicht versehentlich umformatiert/transliteriert.
            # Wir hängen den Hinweis vor dem "Text:"-Block ein, so dass er
            # direkt vor dem zu bereinigenden Text steht.
            names = self.dictionary.correct_names()
            if names:
                keep_line = (
                    "WICHTIG: Behalte die folgenden Eigennamen exakt so wie "
                    "sie sind (Schreibweise, Groß-/Kleinschreibung): "
                    + ", ".join(names) + "."
                )
                if "Text: {text}" in prompt_template:
                    prompt_template = prompt_template.replace(
                        "Text: {text}", keep_line + "\n\nText: {text}",
                    )
                else:
                    prompt_template = keep_line + "\n\n" + prompt_template
            num_predict = max(50, int(word_count * 2.5) + 10)
            payload = json.dumps({
                "model": self.config["ollama_model"],
                "prompt": prompt_template.format(text=text),
                "stream": False,
                "keep_alive": "30m",
                "options": {
                    "temperature": 0,
                    "top_k": 1,
                    "num_predict": num_predict,
                    "num_thread": 8,
                },
            }).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_URL, data=payload,
                headers={"Content-Type": "application/json"},
            )
            t0 = _time.time()
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                cleaned = result.get("response", "").strip()
                log.info(
                    f"Cleanup: {(_time.time() - t0) * 1000:.0f}ms "
                    f"({word_count} Wörter, Modell {self.config['ollama_model']})"
                )
                return cleaned if cleaned else text
        except Exception as e:
            log.warning(f"Cleanup fehlgeschlagen: {e}")
            return text

    def toggle_cleanup(self, _sender):
        if not self.ollama_mgr.is_ready():
            self._notify("IQspeakr", "Ollama läuft nicht. Installiere es im Hauptfenster -> Settings.")
            return
        self.cleanup_enabled = not self.cleanup_enabled
        self.config["cleanup_enabled"] = self.cleanup_enabled
        save_config(self.config)
        self._refresh_menu()

    # --- Hotkey (pynput Global Listener) ---

    def _key_matches(self, key, matcher):
        if isinstance(matcher, set):
            return key in matcher
        if isinstance(matcher, KeyCode) and isinstance(key, KeyCode):
            a = (matcher.char or "").lower() if matcher.char else None
            b = (key.char or "").lower() if key.char else None
            if a is not None and b is not None:
                return a == b
            return matcher == key
        return key == matcher

    def _key_belongs_to_hotkey(self, key):
        """Prüft ob key zu IRGENDEINEM der Hotkey-Matcher gehört -
        sonst müllen wir _pressed_keys mit jeder Taste zu, die der User
        tippt (Performance + Logik-Rauschen)."""
        for matcher in self.hotkey_matchers:
            if isinstance(matcher, set):
                if key in matcher:
                    return True
            else:
                if self._key_matches(key, matcher):
                    return True
        return False

    def _on_key_press(self, key):
        if self._suppress_listener:
            return
        try:
            if self._modifier_only_mode:
                if not self._key_belongs_to_hotkey(key):
                    return
                was_matched_before = self._combo_matches()
                self._pressed_keys.add(key)
                # Erst wenn ALLE Modifier der Kombi gedrückt sind UND der
                # Hold-Modus noch nicht aktiv ist -> Press triggern.
                if not was_matched_before and self._combo_matches() and not self._hold_mode:
                    self._handle_modifier_press()
                return
            self._pressed_keys.add(key)
            if self._combo_matches() and not self._combo_active:
                self._combo_active = True
                self.toggle_recording(None)
        except Exception as e:
            log.error(f"on_key_press Fehler: {e}", exc_info=True)

    def _on_key_release(self, key):
        if self._suppress_listener:
            return
        try:
            if self._modifier_only_mode:
                if not self._key_belongs_to_hotkey(key):
                    return
                was_matched_before = self._combo_matches()
                self._pressed_keys.discard(key)
                # Sobald EINE der Kombi-Tasten losgelassen wurde, gilt die
                # Kombi als "losgelassen" (Release-Handler entscheidet dann
                # über Hold/Tap/Double-Tap).
                if was_matched_before and not self._combo_matches() and self._hold_mode:
                    self._handle_modifier_release()
                return
            self._pressed_keys.discard(key)
            if self._combo_active and not self._combo_matches():
                self._combo_active = False
        except Exception as e:
            log.error(f"on_key_release Fehler: {e}", exc_info=True)

    def _combo_matches(self):
        for matcher in self.hotkey_matchers:
            if isinstance(matcher, set):
                if not any(k in matcher for k in self._pressed_keys):
                    return False
            else:
                if not any(self._key_matches(k, matcher) for k in self._pressed_keys):
                    return False
        return True

    # --- State-Machine für Einzel-Modifier-Hotkey (Hold/Tap/Double-Tap) ---

    def _handle_modifier_press(self):
        now = _time.time()
        log.debug(f"Hotkey GEDRUECKT (t={now:.3f})")

        if self._continuous_mode and self.recording:
            log.info("Daueraufnahme gestoppt (Hotkey gedrückt)")
            self._continuous_mode = False
            self._stop_recording()
            return

        self._ctrl_press_time = now
        self._hold_mode = True
        if not self.recording:
            self._start_recording()

    def _handle_modifier_release(self):
        now = _time.time()
        log.debug(f"Hotkey LOSGELASSEN (t={now:.3f})")
        hold_duration = now - self._ctrl_press_time if self._ctrl_press_time else 0

        if hold_duration < self._hold_threshold:
            if (now - self._last_tap_time) < self._double_tap_window and self._last_tap_time > 0:
                log.info("Doppel-Tap erkannt -> Daueraufnahme-Modus")
                self._continuous_mode = True
                self._hold_mode = False
                self._last_tap_time = 0
                if not self.recording:
                    self._start_recording()
            else:
                log.debug(f"Kurzer Tap ({hold_duration:.2f}s) - warte auf Doppel-Tap")
                self._last_tap_time = now
                self._hold_mode = False
                if self.recording and not self._continuous_mode:
                    self._cancel_recording()
        else:
            if self.recording:
                log.info(f"Hotkey losgelassen nach {hold_duration:.1f}s - transkribiere")
                self._hold_mode = False
                self._stop_recording()

    def _audio_callback(self, indata, frames, time_info, status):
        """Wird vom persistenten Stream kontinuierlich aufgerufen (sounddevice
        worker-Thread). Sammelt Frames nur wenn recording, updated Overlay
        über atomische Attribute."""
        if status:
            log.warning(f"Audio-Status: {status}")
        if not self.recording:
            return
        self.audio_frames.append(indata.copy())
        # Live-Levels für 7-Balken-Overlay (RMS + sqrt-Kurve).
        samples = indata.flatten()
        n = len(samples)
        if n >= PillOverlay.BAR_COUNT:
            seg = n // PillOverlay.BAR_COUNT
            levels = []
            for i in range(PillOverlay.BAR_COUNT):
                chunk = samples[i * seg:(i + 1) * seg]
                rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
                levels.append(min(1.0, float(np.sqrt(rms * 10))))
            self.overlay.set_levels(levels)

    def _cancel_recording(self):
        self.recording = False
        self.audio_frames = []
        self._set_icon_state("ready")
        self._refresh_menu()
        self.overlay_recording_sig.emit(False)
        log.debug("Aufnahme abgebrochen (zu kurzer Tap)")

    def toggle_recording(self, _sender):
        log.info(f"toggle_recording: recording={self.recording}")
        if self.recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if self.model is None:
            log.warning("Modell noch nicht geladen")
            self._notify("IQspeakr", "Modell wird noch geladen, bitte warten...")
            return
        log.info("Aufnahme gestartet")
        self.audio_frames = []
        self.recording = True  # ab jetzt sammelt der Audio-Callback Frames
        self._set_icon_state("rec")
        self._refresh_menu()
        self.overlay_recording_sig.emit(True)

    def _stop_recording(self):
        self.recording = False  # Audio-Callback hört sofort auf zu sammeln
        self._set_icon_state("busy")
        log.info(f"Aufnahme gestoppt, {len(self.audio_frames)} Frames aufgenommen")
        self._refresh_menu()
        self.overlay_recording_sig.emit(False)

        frames = self.audio_frames
        self.audio_frames = []

        if not frames:
            log.warning("Keine Audio-Frames aufgenommen!")
            self._set_icon_state("ready")
            self._refresh_menu()
            return

        threading.Thread(
            target=self._transcribe_frames, args=(frames,), daemon=True,
        ).start()

    def _transcribe_frames(self, frames):
        audio_data = np.concatenate(frames, axis=0).flatten().astype(np.float32)
        audio_dur_sec = len(audio_data) / float(SAMPLE_RATE)
        log.info(
            f"Transkribiere: {len(audio_data)} Samples ({audio_dur_sec:.2f}s), "
            f"Peak: {np.max(np.abs(audio_data)):.4f}"
        )

        try:
            lang = self.config.get("language")
            log.info(f"Starte Whisper-Transkription (Sprache: {lang})...")
            try:
                t_whisper_start = _time.time()
                segments, info = self.model.transcribe(
                    audio_data,
                    language=lang,
                    beam_size=1,           # statt 5: ~halb so lange, minimal weniger Qualität
                    vad_filter=True,       # überspringt Stille-Segmente
                    vad_parameters=dict(min_silence_duration_ms=300),
                    # Kein Cross-Chunk-Kontext: marginal schneller und bei
                    # typisch kurzen Dictate-Samples eh nicht relevant.
                    condition_on_previous_text=False,
                )
                raw_text = "".join(seg.text for seg in segments).strip()
                whisper_ms = (_time.time() - t_whisper_start) * 1000
                log.info(
                    f"Whisper: {whisper_ms:.0f}ms ({whisper_ms / max(audio_dur_sec, 0.01):.1f}x "
                    f"Realtime, Modell {self.config.get('whisper_model')}) "
                    f"-> '{raw_text}'"
                )
            except Exception as e:
                log.error(f"Whisper-Fehler: {e}")
                self._notify("IQspeakr - Fehler", str(e)[:100], level="error")
                return

            if raw_text:
                # Eigennamen-Korrektur vor Cleanup: ersetzt bekannte
                # Falschschreibungen durch die korrekte Form, ohne den Rest
                # anzufassen (siehe _cleanup_text).
                dict_text = self.dictionary.apply(raw_text)
                if dict_text != raw_text:
                    log.info(f"Wörterbuch-Korrektur: '{raw_text}' -> '{dict_text}'")
                text = self._cleanup_text(dict_text)
                log.info(f"Bereinigter Text: '{text}'")
                pyperclip.copy(text)
                log.info("Text in Zwischenablage kopiert")
                self._paste_via_kb(text)
                # History persistiert immer den Endtext (cleaned wenn aktiv,
                # sonst raw). add() emittet changed -> HomeView frischt sich
                # selbst auf.
                try:
                    self.history.add(text)
                except Exception as e:
                    log.warning(f"HistoryStore.add fehlgeschlagen: {e}")
                # Stats für Dashboard: Wortanzahl + Aufnahmedauer.
                try:
                    duration_sec = len(audio_data) / float(SAMPLE_RATE)
                    self.stats.record(
                        int(_time.time()),
                        len(text.split()),
                        duration_sec,
                    )
                except Exception as e:
                    log.warning(f"StatsStore.record fehlgeschlagen: {e}")
            else:
                log.warning("Kein Text erkannt")
                self._notify("IQspeakr", "Kein Text erkannt.")
        except Exception as e:
            log.error(f"Transkriptions-Fehler: {e}", exc_info=True)
        finally:
            self._set_icon_state("ready")
            self._refresh_menu()

    def _paste_via_kb(self, text):
        """Simuliert Ctrl+V. Sperrt währenddessen den eigenen pynput-Listener,
        damit der die simulierten Keys nicht als Hotkey-Press missdeutet
        (Self-Trigger-Bug, der zu paralleler Pseudo-Aufnahme führt)."""
        import time
        # Mini-Delay, damit das OS den Hotkey-Key-Up sauber verarbeitet hat
        # bevor wir Ctrl+V simulieren. 50 ms sind spürbar schneller als die
        # früheren 300 ms und reichen in der Praxis.
        time.sleep(0.05)
        self._suppress_listener = True
        try:
            self._kb_controller.press(Key.ctrl)
            self._kb_controller.press('v')
            self._kb_controller.release('v')
            self._kb_controller.release(Key.ctrl)
            log.info("Ctrl+V via pynput ausgeführt")
        except Exception as e:
            log.error(f"Paste-Fehler: {e}")
        finally:
            # Kleines Delay damit alle Key-Events durch sind, bevor
            # Listener wieder zuhört.
            threading.Timer(0.15, self._unsuppress_listener).start()
        log.info(f"Eingefügt: '{text}'")

    def _unsuppress_listener(self):
        self._suppress_listener = False
        # Pressed-Keys-State zurücksetzen, damit ein dort hängender
        # Modifier nicht beim nächsten echten Press-Event hindert.
        self._pressed_keys.clear()


# =====================================================================
#  Main-Entry: QApplication.exec() hält den Main-Thread.
# =====================================================================

def main():
    # Pflicht unter Windows + PyInstaller wenn torch/whisper multiprocessing
    # irgendwo benutzen - sonst fork-bomb wenn das Bundle sich selbst neu startet.
    import multiprocessing
    multiprocessing.freeze_support()

    log.info(f"main() start - Python {sys.version.split()[0]}")

    # QApplication + Splash existieren bereits aus dem frühen Bootstrap
    # ganz oben in dieser Datei. Hier nur die Referenz übernehmen.
    qapp = _qapp
    splash = _splash
    apply_app_theme(qapp)
    if os.path.exists(APP_ICON_PATH):
        qapp.setWindowIcon(QIcon(APP_ICON_PATH))

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("System-Tray nicht verfügbar - App kann nicht laufen.")
        try:
            splash.close()
        except Exception:
            pass
        QMessageBox.critical(None, "IQspeakr",
                             "Das System-Tray ist nicht verfügbar.")
        sys.exit(1)

    try:
        app = IQspeakrApp(qapp, splash=splash)
    except Exception:
        log.exception("FATAL: IQspeakrApp-Init gescheitert")
        try:
            splash.close()
        except Exception:
            pass
        QMessageBox.critical(
            None, "IQspeakr",
            "App konnte nicht starten. Siehe ~/IQspeakr.log.",
        )
        sys.exit(1)

    # Sicherheitsnetz: Splash spätestens nach 30s schließen, falls Status
    # "Bereit" nie kommt (z.B. Modell-Lade-Fehler).
    QTimer.singleShot(30000, splash.close)

    # Referenz auf `app` am Leben halten, sonst GC's Qt-Tray-Icon weg.
    qapp._iqspeakr_app = app
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
