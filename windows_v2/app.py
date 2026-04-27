#!/usr/bin/env python3
"""
IQspeakr - Lokale Sprache-zu-Text App fuer Windows
(Qt-basiert: PySide6 QSystemTrayIcon + QWidget-Overlay.)
"""

import os

# WICHTIG: Vor torch/whisper-Import setzen. In PyInstaller-Bundles fuehrt
# MKL's default multi-threaded Thread-Pool bei wiederholtem transcribe()
# zuverlaessig zu Access Violations (0xc0000005 / 0xc0000096). Single-Thread
# kostet bei small-Whisper nur ~0.1-0.3s pro Aufnahme, dafuer stabil.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import threading
import subprocess
import tempfile
import json
import sqlite3
import urllib.request
import logging
import sys
import time as _time
from pathlib import Path
from datetime import datetime, date, timedelta

# ffmpeg-PATH (fuer Whisper-Kompatibilitaet zu Assets; unsere Transkription
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
# kann sporadische Crashes in Worker-Threads ausloesen.
if not getattr(sys, "frozen", False) and sys.stderr is not None:
    log.addHandler(logging.StreamHandler(sys.stderr))

# faulthandler liefert bei C-Level-Crashes (Access Violation) Python-Frame +
# Thread-Dump in ein File - unverzichtbar fuer Debugging von torch/MKL-Crashes.
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
        log.info("IQspeakr laeuft bereits - zweite Instanz beendet sich.")
        sys.exit(0)
    _lock_fd.seek(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except SystemExit:
    raise
except Exception as _e:
    log.warning(f"Singleton-Check fehlgeschlagen: {_e}")

# Schwere Imports erst NACH dem Singleton-Check.
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
    QDialog, QLabel, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QListWidget, QListWidgetItem, QStackedWidget, QPushButton,
    QCheckBox, QTextEdit, QPlainTextEdit, QProgressBar, QComboBox,
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
# SQLite fuer das Dashboard - append-only mit potenziell vielen tausend
# Eintraegen, Time-Range-Queries fuer Heatmap. Liegt in %APPDATA%\IQspeakr.
STATS_DB_PATH = os.path.join(APPDATA_DIR, "stats.db")

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

# Asset-Pfad fuer die App-Icon-Datei (in der Sidebar genutzt).
APP_ICON_PATH = os.path.join(APP_DIR, "icon.ico")

# =====================================================================
#  Theme-Tokens. Eine Stelle fuer alle Farben - sonst driftet das auseinander.
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
    setStyleSheet()-Aufrufe in einzelnen Klassen ergaenzen / spezialisieren."""
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
#  brauchen - kein Asset-File noetig, kein extra Package.
# =====================================================================

_LUCIDE_HOME = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>"""

_LUCIDE_TYPE = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 7 4 4 20 4 20 7"/><line x1="9" x2="15" y1="20" y2="20"/><line x1="12" x2="12" y1="4" y2="20"/></svg>"""

_LUCIDE_SETTINGS = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>"""

_LUCIDE_COPY = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>"""

_LUCIDE_CHECK = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>"""

_LUCIDE_BAR_CHART = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>"""


def _lucide_icon(svg_template, size=18, color=None):
    """Rendert einen Lucide-SVG-Template-String zur QIcon. `color` ersetzt
    den `currentColor`-Stroke. Liefert ein QIcon mit der angegebenen Pixel-
    Groesse (HiDPI-aware via QPixmap.devicePixelRatio nicht noetig - bei
    den Sidebar-Sizes faellt sub-pixel-Aliasing nicht ins Auge)."""
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
    Nur sichtbar waehrend Aufnahme - sonst komplett versteckt.
    Audio-Thread schreibt nur atomische Python-Attribute (thread-safe in
    CPython), der QTimer im Main-Thread liest sie - keine Cross-Thread
    Qt-Signals aus C-Callbacks noetig (vermeidet Stack-Races unter
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
        # Timer laeuft nur waehrend Aufnahme - im Idle nichts zu animieren.

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
        # Laeuft nur waehrend Aufnahme. Opacity auf Active-Level faden,
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

        # 7 weisse Balken in der Mitte
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

    # API fuer Audio-Thread: nur atomische Attribut-Zuweisung
    # (in CPython thread-safe). Das Tick im Main-Thread liest.
    def set_levels(self, levels):
        if self.enabled:
            self._levels = list(levels)

    def set_recording(self, on):
        """Main-Thread-only. Overlay nur waehrend Aufnahme sichtbar,
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


CLEANUP_PROMPT_LOCKER = """Du bereinigst gesprochene Sprache minimal-invasiv. WICHTIG: Du DARFST den Text NICHT umformulieren oder paraphrasieren. Der Sprecher soll seinen eigenen Stil wiedererkennen.

ERLAUBT:
- Fuellwoerter entfernen (aehm, aeh, also, sozusagen, halt, quasi, irgendwie, eben, ja, nun)
- Wortdoppelungen und Stotterer entfernen (z.B. "ich ich habe" -> "ich habe")
- Satzzeichen und Grossschreibung korrigieren
- Offensichtliche Grammatikfehler korrigieren (z.B. falsche Artikel, Kasus)
- Kleine Satzumstellungen NUR wenn grammatikalisch notwendig

VERBOTEN:
- Woerter durch Synonyme ersetzen
- Saetze neu formulieren oder glaetten
- Inhalt straffen oder zusammenfassen
- Stil veraendern (umgangssprachlich -> schriftsprachlich)
- Eigene Woerter hinzufuegen

Antworte NUR mit dem bereinigten Text, ohne Erklaerungen, ohne Anfuehrungszeichen.

Text: {text}"""

CLEANUP_PROMPT_FORMAL = """Du bereinigst gesprochene Sprache und ueberfuehrst sie in geschriebenes, foermliches Deutsch. Der Inhalt bleibt vollstaendig erhalten - nur Form und Register werden angehoben.

ERLAUBT:
- Fuellwoerter, Wortdoppelungen und Stotterer entfernen
- Satzzeichen, Grossschreibung und Grammatik korrigieren
- Umgangssprache durch schriftsprachliche Aequivalente ersetzen (z.B. "halt" -> entfernen, "kriegen" -> "erhalten", "ne" -> "eine")
- Saetze umstellen, wenn der Schriftstil das verlangt
- Hoeflichkeitsformen verwenden, wenn aus dem Kontext klar erkennbar
- Verkuerzungen ausschreiben (z.B. "geht's" -> "geht es", "ist's" -> "ist es")

VERBOTEN:
- Inhalt streichen oder zusammenfassen
- Eigene Aussagen hinzufuegen
- Inhaltliche Aussage veraendern

Antworte NUR mit dem bereinigten Text, ohne Erklaerungen, ohne Anfuehrungszeichen.

Text: {text}"""

CLEANUP_PROMPT_SEHR_LOCKER = """Du entfernst nur Spracharten der Pause aus diktierter Sprache. Sonst NICHTS. Der Sprecher will seinen Originaltext exakt 1:1 zurueck, nur ohne Stotterer.

ERLAUBT (und nur das):
- Reine Fuelllaute entfernen: "aehm", "aeh", "oeh", "mhm"
- Direkte Wortdoppelungen entfernen, wenn klar ein Stotterer ist (z.B. "ich ich habe" -> "ich habe", aber NICHT "sehr sehr gut")
- Offensichtliche Satzzeichen am Satzende setzen (Punkt, Fragezeichen)

VERBOTEN:
- Grammatik korrigieren
- Umgangssprache aendern
- Wortdoppelungen entfernen, die zur Betonung dienen
- Gross-/Kleinschreibung anders setzen als im Original (ausser am Satzanfang)
- Satzumstellungen
- Synonyme einsetzen
- Fuellwoerter wie "halt", "also", "quasi" entfernen - die sind Stil

Antworte NUR mit dem unveraenderten Text minus Fuelllaute, ohne Erklaerungen, ohne Anfuehrungszeichen.

Text: {text}"""

# Backward-compatible alias - der alte Name wird ggf. noch referenziert.
CLEANUP_PROMPT = CLEANUP_PROMPT_LOCKER

# Default-Beispiel im "Eigene Anweisungen"-Feld der Individuell-Karte.
CUSTOM_PROMPT_EXAMPLE = """Achte besonders auf Fachbegriffe aus der IT (z.B. "API", "Repository", "Pull Request"). Schreibe diese englischen Begriffe NICHT klein, auch wenn sie im Satzinneren stehen. Behalte du/Sie-Anrede so wie diktiert. Wenn ich eine Aufzaehlung mache, formatiere sie als Bulletpoints."""

# Reihenfolge + Labels fuer die Individuell-Checkboxen. Key matched config.
CUSTOM_OPTIONS = [
    ("filler",     "Fuellwoerter entfernen (aehm, aeh, halt, quasi)"),
    ("repeats",    "Wortdoppelungen / Stotterer entfernen"),
    ("punct",      "Satzzeichen und Grossschreibung korrigieren"),
    ("grammar",    "Offensichtliche Grammatikfehler korrigieren"),
    ("reorder",    "Satzumstellungen erlauben (wenn noetig)"),
    ("formalize",  "Umgangssprache in Schriftsprache ueberfuehren"),
]


def build_custom_prompt(checkboxes, extra_prompt):
    """Generiert aus den Checkbox-Werten + freier Zusatzanweisung den
    Custom-Cleanup-Prompt. checkboxes ist ein dict {key: bool}."""
    rules_allowed = []
    rules_forbidden = []
    if checkboxes.get("filler"):
        rules_allowed.append("- Fuellwoerter entfernen (aehm, aeh, also, sozusagen, halt, quasi, irgendwie, eben, ja, nun)")
    else:
        rules_forbidden.append("- Fuellwoerter entfernen (sie sind Stil)")
    if checkboxes.get("repeats"):
        rules_allowed.append("- Wortdoppelungen und Stotterer entfernen")
    if checkboxes.get("punct"):
        rules_allowed.append("- Satzzeichen und Grossschreibung korrigieren")
    if checkboxes.get("grammar"):
        rules_allowed.append("- Offensichtliche Grammatikfehler korrigieren")
    if checkboxes.get("reorder"):
        rules_allowed.append("- Saetze umstellen, wenn grammatikalisch oder stilistisch noetig")
    else:
        rules_forbidden.append("- Saetze umstellen oder umformulieren")
    if checkboxes.get("formalize"):
        rules_allowed.append("- Umgangssprache durch schriftsprachliche Aequivalente ersetzen")
    else:
        rules_forbidden.append("- Stil veraendern (umgangssprachlich -> schriftsprachlich)")
    # Immer verboten:
    rules_forbidden.append("- Inhalt streichen oder zusammenfassen")
    rules_forbidden.append("- Eigene Aussagen hinzufuegen")

    parts = ["Du bereinigst gesprochene Sprache nach den folgenden Regeln. Inhaltlich nichts hinzufuegen oder entfernen.\n"]
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
    parts.append("Antworte NUR mit dem bereinigten Text, ohne Erklaerungen, ohne Anfuehrungszeichen.")
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
    "formal":      "Ich moechte kurz mitteilen, dass dies grundsaetzlich gut funktioniert.",
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

# Qt-Key -> interner Hotkey-String (fuer Custom-Hotkey-Recorder)
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
    """True wenn die Kombi ausschliesslich aus Modifiern besteht (z.B. 'ctrl',
    'ctrl+shift'). Nur dann sind Hold/Tap/Double-Tap sinnvoll - bei Kombis mit
    Buchstaben oder F-Tasten gibt's naemlich keine klare 'lang gehalten'-Semantik."""
    parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
    if not parts:
        return False
    return all(_is_modifier_name(p) for p in parts)


def _whisper_model_cached(size):
    # faster-whisper laedt Modelle aus Huggingface-Cache (andere Struktur
    # als openai-whisper). Konservativ: True zurueckgeben, dann ueberspringt
    # die UI den "herunterladen?"-Dialog. Download passiert transparent beim
    # ersten load.
    return True


# --- Config ---
DEFAULT_CONFIG = {
    "hotkey": "ctrl+shift",
    "whisper_model": "base",
    "ollama_model": "llama3.2",
    "cleanup_enabled": True,
    "language": "de",
    "overlay_enabled": True,
    # Style-Auswahl fuer die Cleanup-Prompt: formal | locker | sehr_locker | custom
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
    """Einfacher JSON-FIFO-Store fuer die letzten HISTORY_MAX Transkripte.
    Schreibt %APPDATA%\\IQspeakr\\history.json. Emit changed() nach add()
    damit angeschlossene Views (HomeView) sich aktualisieren - ueber Qt-
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
        # mutieren waehrend der Audio-Thread schreibt.
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
#  Stats-Store: SQLite-basierter, append-only Speicher fuer das Dashboard.
#  - Pro Aufnahme ein Row (timestamp, word_count, duration_sec).
#  - Liegt unabhaengig von der 10er-History in stats.db.
#  - duration_sec=0 bedeutet "Dauer unbekannt" (z.B. migrierte
#    History-Eintraege) und wird in WPM-Berechnung ausgeschlossen.
# =====================================================================

class StatsStore(QObject):

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        os.makedirs(APPDATA_DIR, exist_ok=True)
        # check_same_thread=False, weil record() aus dem Audio-/Whisper-
        # Worker-Thread aufgerufen wird. Wir serialisieren manuell ueber
        # _lock - das reicht fuer unseren append-dominanten Workload.
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
        """Wörter pro Minute über alle Eintraege mit duration_sec > 0.
        Liefert None wenn keine Eintraege mit Dauer existieren."""
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
        """Liefert dict[date] -> int fuer die letzten `days_back` Tage,
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
        """Set aller Tage mit mindestens einem Eintrag, ueber die ganze DB."""
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
        """Einmalig beim ersten Start: alte history.json-Eintraege in die
        DB einspielen, damit das Dashboard sofort etwas zu zeigen hat.
        Macht nichts wenn die DB schon Eintraege hat."""
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
            log.info(f"StatsStore: {n} Legacy-History-Eintraege migriert")
        return n


# =====================================================================
#  Ollama-Manager: State-Maschine + Worker fuer Install/Pull/Uninstall.
# =====================================================================

# State-Konstanten - kein Enum, damit die Werte direkt in die Config
# / Logs / UI-Strings wandern koennen ohne .name/.value-Indirektion.
OLLAMA_NOT_INSTALLED = "not_installed"
OLLAMA_DOWNLOADING   = "downloading"
OLLAMA_INSTALLING    = "installing"
OLLAMA_PULLING       = "pulling_model"
OLLAMA_READY         = "ready"
OLLAMA_ERROR         = "error"

# Modell-Optionen fuer den Setup-Dropdown. Reihenfolge = UI-Reihenfolge.
OLLAMA_MODEL_OPTIONS = [
    ("llama3.2",  "llama3.2 (3B) - klein und schnell, Standard"),
    ("llama3.1",  "llama3.1 (8B) - bessere Qualitaet, mehr RAM"),
    ("mistral",   "mistral (7B) - gut fuer Deutsch und Englisch"),
    ("gemma2",    "gemma2 (9B) - Google, solide Qualitaet"),
    ("phi3",      "phi3 (3.8B) - Microsoft, kompakt und gut"),
]


class OllamaManager(QObject):
    """Steuert Detection / Install / Pull / Uninstall des Ollama-Service.

    Alle Worker laufen in eigenen Threads, GUI-Updates ausschliesslich ueber
    Qt-Signals (queued, automatisch im Main-Thread)."""

    state_changed     = Signal(str)             # neuer State-String
    download_progress = Signal(int, int)        # bytes_done, bytes_total
    install_progress  = Signal(str)             # Status-Text waehrend Install
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
        nach Konfig-Aenderungen aufgerufen. current_model: das in config
        eingestellte Modell - wenn vorhanden + Service laeuft -> READY."""
        threading.Thread(
            target=self._refresh_worker,
            args=(current_model,),
            daemon=True,
        ).start()

    def _refresh_worker(self, current_model):
        ok, models = self._ping_service()
        if not ok:
            # Kein Service erreichbar - aber vielleicht installiert aber nicht gestartet?
            if os.path.exists(OLLAMA_EXE):
                # Versuche den Tray-Process von Ollama anzustarten.
                try:
                    subprocess.Popen(
                        [OLLAMA_EXE, "serve"],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    # bis zu 8s warten
                    for _ in range(16):
                        _time.sleep(0.5)
                        ok2, models2 = self._ping_service(timeout=1.5)
                        if ok2:
                            self._set_state(OLLAMA_READY)
                            return
                except Exception as e:
                    log.warning(f"OllamaManager: serve-Start fehlgeschlagen: {e}")
            self._set_state(OLLAMA_NOT_INSTALLED)
            return
        # Service laeuft. READY auch ohne Modell - StyleView prueft separat
        # ob das gewuenschte Modell schon gepullt ist.
        self._set_state(OLLAMA_READY)

    def has_model(self, name):
        """Prueft synchron ob ein Modell schon gepullt ist."""
        ok, models = self._ping_service()
        if not ok:
            return False
        # Modelle koennen mit ":latest" o.ae. ankommen.
        prefixes = [name, name + ":"]
        return any(any(m.startswith(p) for p in prefixes) for m in models)

    # --- Install ---
    def install(self, model_name):
        with self._lock:
            if self._busy:
                self.error_message.emit("Eine andere Aktion laeuft bereits.")
                return
            self._busy = True
        threading.Thread(
            target=self._install_worker,
            args=(model_name,),
            daemon=True,
        ).start()

    def _install_worker(self, model_name):
        try:
            # Schritt 1: Setup-Exe herunterladen.
            self._set_state(OLLAMA_DOWNLOADING)
            tmp_dir = tempfile.mkdtemp(prefix="iqspeakr_ollama_")
            setup_exe = os.path.join(tmp_dir, "OllamaSetup.exe")
            self._download(OLLAMA_INSTALLER_URL, setup_exe)

            # Schritt 2: Silent-Install.
            self._set_state(OLLAMA_INSTALLING)
            self.install_progress.emit("Installation laeuft...")
            self._run_installer(setup_exe)

            # Schritt 3: Auf Service warten.
            self.install_progress.emit("Warte auf Ollama-Service...")
            self._wait_for_service(self.SERVICE_WAIT_SECONDS)

            # Schritt 4: Modell pullen.
            self._set_state(OLLAMA_PULLING)
            self._pull(model_name)

            self._set_state(OLLAMA_READY)
        except Exception as e:
            log.exception("OllamaManager: install fehlgeschlagen")
            self.error_message.emit(str(e))
            self._set_state(OLLAMA_ERROR)
        finally:
            with self._lock:
                self._busy = False

    def _download(self, url, dest_path):
        """Robuster Download mit Range-Resume bei socket.timeout / Verbindungs-
        abbruch. Bis zu DOWNLOAD_RETRIES Versuche; jeder Versuch bei
        urlopen mit DOWNLOAD_TIMEOUT-Sekunden Read-Timeout. Wenn der Server
        Range-Requests unterstuetzt (HTTP 206), wird ab `done` weitergeladen,
        sonst wird die Datei truncated und neu begonnen."""
        import socket as _socket
        max_retries = self.DOWNLOAD_RETRIES
        timeout = self.DOWNLOAD_TIMEOUT
        # Status zum Mitziehen ueber Retries.
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
                        # Server unterstuetzt Resume - Total = bisheriger
                        # Fortschritt + restliche Bytes.
                        if total <= 0:
                            total = done + cl
                        mode = "ab"
                    else:
                        # Kein Resume - Datei neu beginnen.
                        if status != 206 and done > 0:
                            log.warning(
                                f"OllamaManager: Resume nicht unterstuetzt "
                                f"(HTTP {status}), starte Download neu."
                            )
                        done = 0
                        total = cl
                        mode = "wb"
                    with open(dest_path, mode) as f:
                        self.download_progress.emit(done, total)
                        while True:
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
        cmd = [setup_exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
        log.info(f"OllamaManager: starte Installer {cmd}")
        rc = subprocess.call(
            cmd,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
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
        """Public: zieht ein zusaetzliches Modell, vorausgesetzt Ollama
        laeuft schon (state == ready)."""
        with self._lock:
            if self._busy:
                self.error_message.emit("Eine andere Aktion laeuft bereits.")
                return
            self._busy = True
        threading.Thread(
            target=self._pull_only_worker,
            args=(model_name,),
            daemon=True,
        ).start()

    def _pull_only_worker(self, model_name):
        try:
            previous = self._state
            self._set_state(OLLAMA_PULLING)
            self._pull(model_name)
            self._set_state(OLLAMA_READY)
        except Exception as e:
            log.exception("OllamaManager: pull fehlgeschlagen")
            self.error_message.emit(str(e))
            self._set_state(OLLAMA_ERROR)
        finally:
            with self._lock:
                self._busy = False

    # --- Uninstall ---
    def uninstall(self):
        with self._lock:
            if self._busy:
                self.error_message.emit("Eine andere Aktion laeuft bereits.")
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
            # zwangslaeufig ein Fehler - wir verifizieren ueber Service-Status.
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
#  Hotkey-Recorder: Dialog, der Tastendruecke live aufnimmt.
# =====================================================================

class HotkeyRecorderDialog(QDialog):
    """Modaler Dialog zum Aufnehmen einer Tastenkombination.

    Der User druckt einfach die gewuenschte Kombi (z.B. Strg+Umschalt), laesst
    los, der Dialog zeigt das Ergebnis und speichert es beim Klick auf
    "Speichern"."""

    _MOD_ORDER = ("ctrl", "alt", "shift", "win")

    # Heuristik: Kombis die wir als System-/App-kritisch flaggen. Kein
    # hartes Verbot, nur Confirm vor dem Speichern.
    _CONFLICT_COMBOS = {
        "ctrl+c":     "Kopieren",
        "ctrl+v":     "Einfuegen (wird intern fuer Auto-Paste benutzt!)",
        "ctrl+x":     "Ausschneiden",
        "ctrl+z":     "Rueckgaengig",
        "ctrl+y":     "Wiederholen",
        "ctrl+s":     "Speichern",
        "ctrl+a":     "Alles auswaehlen",
        "ctrl+f":     "Suchen",
        "alt+tab":    "Fenster wechseln",
        "alt+f4":     "Fenster schliessen",
        "win+l":      "Computer sperren",
        "win+d":      "Desktop anzeigen",
        "win+e":      "Explorer oeffnen",
    }

    def __init__(self, parent=None, initial=""):
        super().__init__(parent)
        self.setWindowTitle("Tastenkombination aendern")
        self.setModal(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.resize(520, 280)
        if os.path.exists(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))

        self._mods = set()
        self._key = None
        self._capture_complete = False
        self._captured = (initial or "").strip().lower()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(14)

        head = QLabel("Druecke die gewuenschte Tastenkombination")
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
            self._display.setText("(druecke eine Taste)")
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
        # ueberschreiben lassen. Kein Hard-Block.
        warn = self._CONFLICT_COMBOS.get(combo)
        if warn:
            confirm = QMessageBox.question(
                self,
                "Konflikt mit Standard-Shortcut",
                f"\"{hotkey_display(combo)}\" ist normalerweise reserviert fuer:\n"
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
        if qt_key in _QT_MOD_TO_NAME:
            self._mods.add(_QT_MOD_TO_NAME[qt_key])
        elif qt_key in _QT_NAMED_KEYS:
            self._key = _QT_NAMED_KEYS[qt_key]
        else:
            t = (event.text() or "").lower()
            if len(t) == 1 and t.isalnum():
                self._key = t
        self._refresh_display()
        event.accept()

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        qt_key = event.key()
        m = int(QApplication.keyboardModifiers())
        still_mod_down = bool(
            m & int(Qt.ControlModifier) | m & int(Qt.ShiftModifier)
            | m & int(Qt.AltModifier) | m & int(Qt.MetaModifier)
        )
        # Capture finalizen sobald a) ein Nicht-Modifier losgelassen wurde
        # oder b) nach dem Loslassen keine Modifier mehr aktiv sind.
        if (qt_key not in _QT_MOD_TO_NAME) or not still_mod_down:
            combo = self._build_combo_str()
            if combo:
                self._captured = combo
                self._capture_complete = True
                self._refresh_display()
        event.accept()

    def result_combo(self):
        return self._captured


# =====================================================================
#  Detail-Dialog fuer einen History-Eintrag.
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
        close_btn = QPushButton("Schliessen")
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
        # kurzes visuelles Feedback - 1 s lang Check-Icon, dann zurueck.
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

        sub = QLabel(f"Die letzten {HISTORY_MAX} Transkripte. Aelteste fliegen automatisch raus.")
        sub.setProperty("role", "sub")
        layout.addWidget(sub)
        layout.addSpacing(18)

        # Card-Container fuer die Liste der Eintraege - wird gefuellt
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

        # ScrollArea umschliesst den Card-Container, damit lange History
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
            empty = QLabel("Noch keine Aufnahmen.\nHalte deinen Hotkey gedrueckt und sprich.")
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
    """Eine Kennzahl-Card: grosser Wert, Label, optional Sub-Label."""

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


class HeatmapWidget(QWidget):
    """12 Wochen Activity-Heatmap, GitHub-Style. Mouseover setzt Tooltip
    'X Aufnahmen am DD.MM.YYYY'. Spalten = Wochen (Mo bis So), Zeilen =
    Wochentage. Die rechte Spalte enthaelt die aktuelle (oft unvollstaendige)
    Woche, die linke die aelteste der 12-Wochen-Range."""

    WEEKS = 12
    DAYS = 7
    CELL = 14
    GAP = 4
    LEFT_PAD = 36     # Platz fuer Wochentag-Labels
    TOP_PAD = 22      # Platz fuer Monatslabels
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
        self._cells = []      # [(QRect, date, count), ...] fuer Mouse-Lookup
        self.setMouseTracking(True)
        # Hoehe = Top-Pad + 7 Zeilen + 6 Gaps + Legenden-Bereich
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
        # rechts (juengste Woche, endet am letzten Sonntag).
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
        # einen Monatsanfang (Tag 1-7 = erste Woche) enthaelt.
        last_month = None
        for col in range(self.WEEKS):
            week_start = start_monday + timedelta(days=col * 7)
            # Pruefe ob in dieser Woche der Monatsanfang liegt
            for d in range(7):
                day = week_start + timedelta(days=d)
                if day.day <= 7 and day.month != last_month:
                    last_month = day.month
                    x = self.LEFT_PAD + col * (self.CELL + self.GAP)
                    p.drawText(x, self.TOP_PAD - 8,
                               ["Jan", "Feb", "Maer", "Apr", "Mai", "Jun",
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

        # Stat-Cards-Reihe (3 nebeneinander, stretch=1 fuer responsive width)
        self._cards_row = QHBoxLayout()
        self._cards_row.setSpacing(16)
        self._wpm_card = StatCard("Woerter pro Minute", "—")
        self._words_card = StatCard("Woerter insgesamt", "0")
        self._streak_card = StatCard("Tage Streak", "0", sub="Laengster Streak: 0 Tage")
        self._cards_row.addWidget(self._wpm_card, 1)
        self._cards_row.addWidget(self._words_card, 1)
        self._cards_row.addWidget(self._streak_card, 1)
        sp.addLayout(self._cards_row)

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
        hc_title = QLabel("Aktivitaet (letzte 12 Wochen)")
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

        # Woerter total - Tausender mit Punkt (DE-Konvention).
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
    """Eine klickbare Karte fuer einen Style. Selected-State steuert die
    Border-Farbe ueber stylesheet-property 'selected'."""

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

        lock_text = QLabel(
            "Aktiviere KI-Textbereinigung in den Einstellungen.\n"
            "Ohne Ollama gibt die App den Roh-Whisper-Text aus."
        )
        lock_text.setAlignment(Qt.AlignCenter)
        lock_text.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
        lock_text.setWordWrap(True)
        lb_layout.addWidget(lock_text)
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

        cards_row = QHBoxLayout()
        cards_row.setSpacing(14)
        for key, label in (
            ("formal",      "Foermlich"),
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
            cards_row.addWidget(card, 1)
            self._cards[key] = card
        cp.addLayout(cards_row)

        cp.addSpacing(16)

        # Individuell-Editor (sichtbar wenn style==custom)
        self._custom_box = QGroupBox("Individuell-Konfiguration")
        cb_layout = QVBoxLayout(self._custom_box)
        cb_layout.setSpacing(6)
        cb_layout.setContentsMargins(4, 8, 4, 4)

        self._checkbox_widgets = {}
        for key, label in CUSTOM_OPTIONS:
            cb = QCheckBox(label)
            cb.toggled.connect(self._save_custom)
            cb_layout.addWidget(cb)
            self._checkbox_widgets[key] = cb

        cb_layout.addSpacing(10)
        custom_label = QLabel("Eigene Anweisungen (werden an die obigen Regeln angehaengt):")
        custom_label.setProperty("role", "muted")
        cb_layout.addWidget(custom_label)
        self._extra_edit = QPlainTextEdit()
        self._extra_edit.setPlaceholderText(CUSTOM_PROMPT_EXAMPLE)
        self._extra_edit.setMinimumHeight(120)
        cb_layout.addWidget(self._extra_edit)

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
        log.info(f"StyleView: style geaendert auf '{key}'")

    def _save_custom(self, *_args):
        cust = self.app.config.get("style_custom") or {}
        cust["checkboxes"] = {
            k: bool(w.isChecked()) for k, w in self._checkbox_widgets.items()
        }
        cust["extra_prompt"] = self._extra_edit.toPlainText().strip()
        self.app.config["style_custom"] = cust
        save_config(self.app.config)

    def refresh_lock(self):
        """Page 0 vs Page 1 abhaengig vom Ollama-State umschalten."""
        ready = self.app.ollama_mgr.is_ready()
        self._stack.setCurrentIndex(1 if ready else 0)


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
        hot_btn = QPushButton("Aendern...")
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
            ("small",  "small - Gute Qualitaet (~465 MB)"),
            ("medium", "medium - Beste Qualitaet (~1.5 GB)"),
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

        self._overlay_cb = QCheckBox("Pill-Overlay waehrend Aufnahme anzeigen")
        self._overlay_cb.setChecked(bool(self.app.config.get("overlay_enabled", True)))
        self._overlay_cb.toggled.connect(self._on_overlay_toggled)
        gl.addRow(self._form_label(""), self._overlay_cb)

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

        # Modell-Auswahl: zwei Rollen je nach State.
        # not_installed: das Modell wird beim Erstinstall mitgepullt.
        # ready: das Modell wird zum aktiven Cleanup-Modell, evtl. Pull triggern.
        self._model_combo = QComboBox()
        for key, label in OLLAMA_MODEL_OPTIONS:
            self._model_combo.addItem(label, key)
        cur_m = self.app.config.get("ollama_model", "llama3.2")
        for i in range(self._model_combo.count()):
            if self._model_combo.itemData(i) == cur_m:
                self._model_combo.setCurrentIndex(i)
                break
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)

        model_form = QFormLayout()
        model_form.setHorizontalSpacing(20)
        model_form.setVerticalSpacing(10)
        model_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        model_form.addRow(self._form_label("Modell"), self._model_combo)
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

        v.addWidget(self._ollama_box)

        v.addStretch(1)

        # Signals vom OllamaManager
        self.app.ollama_mgr.state_changed.connect(self._on_state_changed)
        self.app.ollama_mgr.download_progress.connect(self._on_download_progress)
        self.app.ollama_mgr.install_progress.connect(self._on_install_text)
        self.app.ollama_mgr.pull_progress.connect(self._on_pull_progress)
        self.app.ollama_mgr.error_message.connect(self._on_error)

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
        # Konstruktor-Signatur: (parent, initial). Frueher waren die
        # Argumente vertauscht -> QDialog bekam einen str als parent
        # und der Klick lief in einen geschluckten TypeError.
        dlg = HotkeyRecorderDialog(self, self.app.config.get("hotkey", ""))
        if dlg.exec() == QDialog.Accepted:
            combo = dlg.result_combo()
            if combo:
                self.app.config["hotkey"] = combo
                save_config(self.app.config)
                self._hotkey_label.setText(hotkey_display(combo))
                self.app._reload_hotkey()

    def _on_whisper_changed(self, _idx):
        size = self._whisper_combo.currentData()
        if not size:
            return
        self.app.config["whisper_model"] = size
        save_config(self.app.config)
        self.app._reload_whisper_model()

    def _on_language_changed(self, _idx):
        code = self._lang_combo.currentData()
        if not code:
            return
        self.app.config["language"] = code
        save_config(self.app.config)

    def _on_overlay_toggled(self, on):
        self.app.config["overlay_enabled"] = bool(on)
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
        self.app.config["ollama_model"] = new_model
        save_config(self.app.config)
        self.app.rebuild_menu_sig.emit()
        # Wenn Ollama schon laeuft, Modell ggf. pullen.
        if self.app.ollama_mgr.is_ready():
            if not self.app.ollama_mgr.has_model(new_model):
                self.app.ollama_mgr.pull_model(new_model)

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
        if state == OLLAMA_NOT_INSTALLED or state == OLLAMA_ERROR:
            self.app.ollama_mgr.install(model)
        elif state == OLLAMA_READY:
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
        # Sichtbarkeit Default zuruecksetzen
        self._progress.setVisible(False)
        self._progress_text.setVisible(False)
        if state == OLLAMA_NOT_INSTALLED:
            self._ollama_status.setText(
                "Ollama ist nicht installiert. Installiere es, um KI-"
                "Textbereinigung zu aktivieren (Download ca. 1.8 GB)."
            )
            self._ollama_status.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
            self._action_btn.setText("Ollama herunterladen und installieren")
            self._set_action_role("primary")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(True)
            self._cleanup_cb.setEnabled(False)
        elif state == OLLAMA_DOWNLOADING:
            self._ollama_status.setText("Lade OllamaSetup.exe herunter...")
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Wird installiert...")
            self._set_action_role("primary")
            self._action_btn.setEnabled(False)
            self._model_combo.setEnabled(False)
            self._cleanup_cb.setEnabled(False)
            self._progress.setVisible(True)
            self._progress_text.setVisible(True)
        elif state == OLLAMA_INSTALLING:
            self._ollama_status.setText("Installer laeuft...")
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Wird installiert...")
            self._set_action_role("primary")
            self._action_btn.setEnabled(False)
            self._model_combo.setEnabled(False)
            self._cleanup_cb.setEnabled(False)
            self._progress.setRange(0, 0)
            self._progress.setVisible(True)
            self._progress_text.setVisible(True)
        elif state == OLLAMA_PULLING:
            self._ollama_status.setText("Lade Modell herunter...")
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Modell wird geladen...")
            self._set_action_role("primary")
            self._action_btn.setEnabled(False)
            self._model_combo.setEnabled(False)
            self._cleanup_cb.setEnabled(False)
            self._progress.setVisible(True)
            self._progress_text.setVisible(True)
        elif state == OLLAMA_READY:
            self._ollama_status.setText("Ollama aktiv. KI-Textbereinigung verfuegbar.")
            self._ollama_status.setStyleSheet(f"color: {THEME_SUCCESS}; font-weight: 500;")
            self._action_btn.setText("Ollama deinstallieren")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(True)
            self._cleanup_cb.setEnabled(True)
        else:  # OLLAMA_ERROR
            self._ollama_status.setText("Fehler. Versuche es erneut.")
            self._ollama_status.setStyleSheet(f"color: {THEME_DANGER};")
            self._action_btn.setText("Erneut versuchen")
            self._set_action_role("primary")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(True)
            self._cleanup_cb.setEnabled(False)


# =====================================================================
#  Main-Window: Sidebar + QStackedWidget mit drei Views.
# =====================================================================

class MainWindow(QMainWindow):
    # (key, label, lucide-svg). Reihenfolge = UI-Reihenfolge.
    NAV_ITEMS = [
        ("home",      "Home",      _LUCIDE_HOME),
        ("dashboard", "Dashboard", _LUCIDE_BAR_CHART),
        ("style",     "Style",     _LUCIDE_TYPE),
    ]

    # Sidebar-QSS: 11/14-Padding, 6px-Radius, Hover-Tint, Active mit
    # 3px Akzent-Strich links + kraeftigerer Schrift. padding-left wird
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
        sw_layout.setContentsMargins(0, 0, 0, 0)
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

        # --- Trenner ueber Settings (sehr dezent, low-opacity) ---
        bottom_sep = QFrame()
        bottom_sep.setFixedHeight(1)
        bottom_sep.setStyleSheet(f"background: {THEME_BORDER_SOFT};")
        sw_layout.addWidget(bottom_sep)

        self._settings_list = QListWidget()
        self._settings_list.setObjectName("SidebarNav")
        self._settings_list.setStyleSheet(self.SIDEBAR_LIST_QSS)
        self._settings_list.setFixedHeight(54)
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
        self.settings_view = SettingsView(self.app)

        self._stack.addWidget(self.home_view)
        self._stack.addWidget(self.dashboard_view)
        self._stack.addWidget(self.style_view)
        self._stack.addWidget(self.settings_view)

        # Mapping nav-key -> stack-index
        self._nav_idx = {"home": 0, "dashboard": 1, "style": 2, "settings": 3}

        self._sidebar.currentRowChanged.connect(self._on_top_nav_changed)
        self._settings_list.itemClicked.connect(self._on_settings_clicked)
        self._sidebar.setCurrentRow(0)

        # Reagier auf Ollama-State um Style-Sperre umzuschalten
        self.app.ollama_mgr.state_changed.connect(self._on_ollama_state)

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

    def closeEvent(self, event):
        # Schliessen versteckt nur, App lebt im Tray weiter.
        self.hide()
        event.ignore()


# =====================================================================
#  Haupt-App: QObject mit Signals fuer thread-safe GUI-Updates.
# =====================================================================

class IQspeakrApp(QObject):

    # Signals, die Worker-Threads emittieren koennen, um die GUI
    # (tray-icon, menue, notifications) im Main-Thread zu aktualisieren.
    icon_state_sig = Signal(str)
    rebuild_menu_sig = Signal()
    notify_sig = Signal(str, str)
    status_sig = Signal(str)
    # Overlay-Show/Hide MUSS ueber Signal laufen, nicht direkt: _start_recording
    # / _stop_recording werden aus dem pynput-Listener-Thread aufgerufen, und
    # QWidget.show()/hide() greifen nur im Main-Thread.
    overlay_recording_sig = Signal(bool)

    def __init__(self, qapp):
        super().__init__()
        self.qapp = qapp
        self.config = load_config()
        self.recording = False
        self.audio_frames = []
        # Persistenter Audio-Stream: einmal geoeffnet, lebt bis zum Quit.
        # Vermeidet PortAudio-Races bei rapidem open/close.
        self._persistent_stream = None
        # Sperrt Listener kurzzeitig waehrend wir Ctrl+V simulieren - sonst
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

        # History + Stats + Ollama-Manager. main_window wird lazy beim
        # ersten Open instanziiert.
        self.history = HistoryStore(self)
        self.stats = StatsStore(self)
        # Einmal-Migration: alte history.json-Eintraege ins Dashboard,
        # damit es nicht mit Empty-State startet.
        try:
            self.stats.import_legacy_history(self.history.items())
        except Exception as e:
            log.warning(f"Stats-Migration fehlgeschlagen: {e}")
        self.ollama_mgr = OllamaManager(self)
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
        log.info("pynput Keyboard-Listener aktiv - Hotkey-Erkennung laeuft")

    # --- Slots (laufen immer im Main-Thread) ---

    def _on_icon_state(self, state):
        try:
            self.tray.setIcon(QIcon(_make_icon_pixmap(state)))
        except Exception as e:
            log.warning(f"Icon-Update fehlgeschlagen: {e}")

    def _on_notify(self, title, message):
        try:
            self.tray.showMessage(title, message, QSystemTrayIcon.Information, 4000)
        except Exception as e:
            log.warning(f"Notification fehlgeschlagen: {e}")

    def _on_status(self, text):
        self._status_text = text
        self.rebuild_menu_sig.emit()

    # Thin wrappers, damit Call-Sites wie vorher bleiben koennen.
    def _set_icon_state(self, state):
        self.icon_state_sig.emit(state)

    def _notify(self, title, message):
        self.notify_sig.emit(title, message)

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
        # DoubleClick auf Tray-Icon oeffnet das Hauptfenster.
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
            # Ollama frisch verfuegbar -> Cleanup wieder aktivieren falls
            # User es eingeschaltet hatte.
            self.cleanup_enabled = bool(self.config.get("cleanup_enabled", True))
        self.rebuild_menu_sig.emit()

    # --- Hilfsmethoden, von der SettingsView aufgerufen ---

    def _reload_hotkey(self):
        """Hotkey-Listener auf den neuen Wert in self.config umstellen."""
        try:
            self._listener.stop()
        except Exception:
            pass
        self.hotkey_matchers = parse_hotkey(self.config["hotkey"])
        self.hotkey_label = hotkey_display(self.config["hotkey"])
        self._modifier_only_mode = _hotkey_is_all_modifiers(self.config["hotkey"])
        self._pressed_keys.clear()
        self._listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self._listener.daemon = True
        self._listener.start()
        self._refresh_menu()

    def _reload_whisper_model(self):
        """Whisper-Modell neu laden (im Hintergrund-Thread). Unterbindet
        Aufnahmen waehrend des Ladens, damit niemand in ein None-Modell hineinruft."""
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

    # --- Menue-Bau ---

    def _build_menu(self):
        """QMenu neu aufbauen. Wird bei jedem Config-Change aufgerufen."""
        self._menu.clear()

        # Hauptfenster oeffnen - vor allen anderen Eintraegen, damit es
        # auf den ersten Blick erreichbar ist.
        act_open = QAction("Hauptfenster oeffnen", self._menu)
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

        # KI-Bereinigung (nur wenn Ollama laeuft)
        if self.ollama_available:
            lbl = f"KI-Bereinigung: {'An' if self.cleanup_enabled else 'Aus'}"
            act_cleanup = QAction(lbl, self._menu)
            act_cleanup.triggered.connect(lambda _=False: self.toggle_cleanup(None))
            self._menu.addAction(act_cleanup)

        # Einstellungen-Untermenue
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

        cfg_act = QAction("Konfig-Datei oeffnen", self._menu)
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
        # Wenn der aktuelle Hotkey keine Preset-Option ist, zaehlt er als "custom".
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
            ("small", "small - Gute Qualitaet (~465 MB)"),
            ("medium", "medium - Empfohlen, beste Qualitaet (~1.5 GB)"),
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
            ("llama3.1", "llama3.1 - Bessere Qualitaet (8B)"),
            ("mistral", "mistral - Gut fuer Deutsch/Europaeisch (7B)"),
            ("gemma2", "gemma2 - Google, solide Qualitaet (9B)"),
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

    def _apply_hotkey(self, hotkey_str):
        self.config["hotkey"] = hotkey_str
        self.hotkey_matchers = parse_hotkey(hotkey_str)
        self.hotkey_label = hotkey_display(hotkey_str)
        self._modifier_only_mode = _hotkey_is_all_modifiers(hotkey_str)
        self._pressed_keys.clear()
        self._combo_active = False
        self._hold_mode = False
        self._continuous_mode = False
        save_config(self.config)
        self._refresh_menu()
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
            self._notify("IQspeakr", f"Ungueltige Kombination: {hotkey_str}")

    def _make_whisper_callback(self, size):
        def cb(_checked=False):
            if size == self.config["whisper_model"]:
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
            threading.Thread(target=self._load_model, daemon=True).start()
        return cb

    def _make_ollama_callback(self, model_name):
        def cb(_checked=False):
            self.config["ollama_model"] = model_name
            save_config(self.config)
            self._refresh_menu()
            self._notify("IQspeakr", f"Ollama-Modell geaendert: {model_name}")
        return cb

    def _make_lang_callback(self, lang_code):
        def cb(_checked=False):
            self.config["language"] = lang_code
            save_config(self.config)
            self._refresh_menu()
            lbl = "Automatisch" if lang_code is None else lang_code
            self._notify("IQspeakr", f"Sprache geaendert: {lbl}")
        return cb

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
            self._notify("IQspeakr - Fehler", "Modell-Laden gescheitert. Siehe IQspeakr.log.")
            return

        # Persistenten Audio-Stream oeffnen - bleibt bis zum Quit aktiv.
        try:
            self._persistent_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self._audio_callback,
            )
            self._persistent_stream.start()
            log.info("Persistenter Audio-Stream gestartet")
        except Exception as e:
            log.error(f"Audio-Stream-Init-Fehler: {e}")
        # Ollama-Status asynchron pruefen (kein Blocken des Modell-Lade-
        # Threads). _on_ollama_state_changed setzt self.ollama_available.
        self.ollama_mgr.refresh_state(self.config.get("ollama_model"))
        self._set_status("Bereit")
        self._notify(
            "IQspeakr - Modell geladen",
            f"Whisper '{self.config['whisper_model']}' ist bereit.\n"
            f"{self.hotkey_label} gedrueckt halten = Aufnahme\n"
            f"{self.hotkey_label} 2x tippen = Daueraufnahme",
        )

    def _cleanup_text(self, text):
        """Cleanup via Ollama, falls aktiviert + Service erreichbar.
        Prompt wird aus dem aktuell gewaehlten Style gebaut."""
        if not self.cleanup_enabled or not self.ollama_mgr.is_ready():
            return text
        try:
            prompt_template = get_cleanup_prompt(self.config)
            payload = json.dumps({
                "model": self.config["ollama_model"],
                "prompt": prompt_template.format(text=text),
                "stream": False,
                "options": {"temperature": 0.1, "top_p": 0.5},
            }).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_URL, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                cleaned = result.get("response", "").strip()
                return cleaned if cleaned else text
        except Exception as e:
            log.warning(f"Cleanup fehlgeschlagen: {e}")
            return text

    def toggle_cleanup(self, _sender):
        if not self.ollama_mgr.is_ready():
            self._notify("IQspeakr", "Ollama laeuft nicht. Installiere es im Hauptfenster -> Settings.")
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
        """Prueft ob key zu IRGENDEINEM der Hotkey-Matcher gehoert -
        sonst muellen wir _pressed_keys mit jeder Taste zu, die der User
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
                # Erst wenn ALLE Modifier der Kombi gedrueckt sind UND der
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
                # ueber Hold/Tap/Double-Tap).
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

    # --- State-Machine fuer Einzel-Modifier-Hotkey (Hold/Tap/Double-Tap) ---

    def _handle_modifier_press(self):
        now = _time.time()
        log.debug(f"Hotkey GEDRUECKT (t={now:.3f})")

        if self._continuous_mode and self.recording:
            log.info("Daueraufnahme gestoppt (Hotkey gedrueckt)")
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
        ueber atomische Attribute."""
        if status:
            log.warning(f"Audio-Status: {status}")
        if not self.recording:
            return
        self.audio_frames.append(indata.copy())
        # Live-Levels fuer 7-Balken-Overlay (RMS + sqrt-Kurve).
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
        self.recording = False  # Audio-Callback hoert sofort auf zu sammeln
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
        log.info(f"Transkribiere: {len(audio_data)} Samples, Peak: {np.max(np.abs(audio_data)):.4f}")

        try:
            lang = self.config.get("language")
            log.info(f"Starte Whisper-Transkription (Sprache: {lang})...")
            try:
                segments, info = self.model.transcribe(
                    audio_data,
                    language=lang,
                    beam_size=1,           # statt 5: ~halb so lange, minimal weniger Qualitaet
                    vad_filter=True,       # ueberspringt Stille-Segmente
                    vad_parameters=dict(min_silence_duration_ms=300),
                    # Kein Cross-Chunk-Kontext: marginal schneller und bei
                    # typisch kurzen Dictate-Samples eh nicht relevant.
                    condition_on_previous_text=False,
                )
                raw_text = "".join(seg.text for seg in segments).strip()
                log.info(f"Whisper-Ergebnis: '{raw_text}' (Sprache: {info.language})")
            except Exception as e:
                log.error(f"Whisper-Fehler: {e}")
                self._notify("IQspeakr - Fehler", str(e)[:100])
                return

            if raw_text:
                text = self._cleanup_text(raw_text)
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
                # Stats fuer Dashboard: Wortanzahl + Aufnahmedauer.
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
        """Simuliert Ctrl+V. Sperrt waehrenddessen den eigenen pynput-Listener,
        damit der die simulierten Keys nicht als Hotkey-Press missdeutet
        (Self-Trigger-Bug, der zu paralleler Pseudo-Aufnahme fuehrt)."""
        import time
        # Mini-Delay, damit das OS den Hotkey-Key-Up sauber verarbeitet hat
        # bevor wir Ctrl+V simulieren. 50 ms sind spuerbar schneller als die
        # frueheren 300 ms und reichen in der Praxis.
        time.sleep(0.05)
        self._suppress_listener = True
        try:
            self._kb_controller.press(Key.ctrl)
            self._kb_controller.press('v')
            self._kb_controller.release('v')
            self._kb_controller.release(Key.ctrl)
            log.info("Ctrl+V via pynput ausgefuehrt")
        except Exception as e:
            log.error(f"Paste-Fehler: {e}")
        finally:
            # Kleines Delay damit alle Key-Events durch sind, bevor
            # Listener wieder zuhoert.
            threading.Timer(0.15, self._unsuppress_listener).start()
        log.info(f"Eingefuegt: '{text}'")

    def _unsuppress_listener(self):
        self._suppress_listener = False
        # Pressed-Keys-State zuruecksetzen, damit ein dort haengender
        # Modifier nicht beim naechsten echten Press-Event hindert.
        self._pressed_keys.clear()


# =====================================================================
#  Main-Entry: QApplication.exec() haelt den Main-Thread.
# =====================================================================

def main():
    # Pflicht unter Windows + PyInstaller wenn torch/whisper multiprocessing
    # irgendwo benutzen - sonst fork-bomb wenn das Bundle sich selbst neu startet.
    import multiprocessing
    multiprocessing.freeze_support()

    qapp = QApplication(sys.argv)
    # Verhindert dass das Schliessen des (unsichtbaren) Overlay-Fensters
    # die gesamte App beendet.
    qapp.setQuitOnLastWindowClosed(False)
    apply_app_theme(qapp)
    if os.path.exists(APP_ICON_PATH):
        qapp.setWindowIcon(QIcon(APP_ICON_PATH))

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("System-Tray nicht verfuegbar - App kann nicht laufen.")
        QMessageBox.critical(None, "IQspeakr",
                             "Das System-Tray ist nicht verfuegbar.")
        sys.exit(1)

    app = IQspeakrApp(qapp)
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
