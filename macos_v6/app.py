#!/usr/bin/env python3
"""
IQspeakr - Lokale Sprache-zu-Text App für macOS (v2)
(Qt-basiert: PySide6 QSystemTrayIcon + QWidget-Overlay.)

Portiert aus windows/app.py. Plattform-spezifische Teile (Singleton-Lock,
PATH-Setup, Paste-Key, Dock-Icon) sind Mac-nativ. STT nutzt openai-whisper
mit automatischer MPS-Auswahl (Apple-Silicon-GPU).
"""

import os

# ffmpeg-PATH (openai-whisper braucht ffmpeg für manche Dateiformate nicht,
# wenn wir numpy-Arrays reinreichen — der PATH-Fix hier ist aber trotzdem
# gut, damit eventuelle Sub-Tools (und Cache-Tools) ffmpeg finden).
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    _BUNDLE_BIN = str(Path(sys._MEIPASS) / "bin")
    if _BUNDLE_BIN not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _BUNDLE_BIN + os.pathsep + os.environ.get("PATH", "")

# Mac-typische Orte + unser eigener bin-Ordner (wie in der alten Mac-Version).
_PATH_PARTS = [
    str(Path.home() / ".iqspeakr" / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]
for _p in _PATH_PARTS:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

import threading
import subprocess
import json
import re
import sqlite3
import tarfile
import urllib.request
import urllib.error
import logging
import time as _time
from datetime import datetime, date, timedelta

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

# faulthandler liefert bei C-Level-Crashes Python-Frame + Thread-Dump.
# Nützlich zur Diagnose von CoreAudio-/Torch-Problemen.
import faulthandler
_fh_file = open(str(Path.home() / "IQspeakr.crash.log"), "w", buffering=1)
faulthandler.enable(file=_fh_file, all_threads=True)

# --- Singleton: nur eine Instanz erlauben ---
# fcntl.flock(LOCK_EX | LOCK_NB) ist atomar — nur ein Prozess bekommt den
# Lock, alle weiteren scheitern sofort mit BlockingIOError. Der Lock wird
# vom Kernel bei Prozess-Ende automatisch freigegeben (auch bei kill -9).
# WICHTIG: Der File-Descriptor MUSS als Modul-Global bestehen bleiben —
# sobald GC ihn einsammelt, gibt der Kernel den Lock frei.
import fcntl

_LOCK_FILE = "/tmp/iqspeakr.pid"
try:
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("IQspeakr laeuft bereits - zweite Instanz beendet sich.")
        sys.exit(0)
    # Lock erworben - PID reinschreiben (informativ für `ps`/Debugging).
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except SystemExit:
    raise
except Exception as _e:
    log.warning(f"Singleton-Check fehlgeschlagen: {_e}")

# =====================================================================
#  Früher Splash: MUSS vor den schweren Imports (whisper, torch,
#  sounddevice, pynput) gezeigt werden, weil die zusammen 1-3 Sekunden
#  brauchen. Ohne diesen Block sieht der User in der Zeit nichts und
#  denkt, das Doppelklicken hätte nicht funktioniert.
# =====================================================================
from PySide6.QtCore import Qt as _Qt, QCoreApplication as _QCoreApplication
from PySide6.QtWidgets import (
    QApplication as _QApplication,
    QWidget as _QWidget,
    QLabel as _QLabel,
    QProgressBar as _QProgressBar,
    QVBoxLayout as _QVBoxLayout,
)
from PySide6.QtGui import QGuiApplication as _QGuiApplication

# WICHTIG: macOS-Standard von Qt vertauscht in keyEvents physische
# Ctrl- und Cmd-Tasten (damit "Ctrl+C" plattformübergreifend funktioniert
# heißt auf Mac de facto "Cmd+C"). Für den Hotkey-Recorder wollen wir
# aber die PHYSISCHE Taste sehen (User druckt ctrl -> wir lesen ctrl).
# Dieses Attribut MUSS vor der QApplication-Erstellung gesetzt werden,
# sonst wirkt es nicht.
_QCoreApplication.setAttribute(_Qt.AA_MacDontSwapCtrlAndMeta, True)


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
# openai-whisper (nicht faster-whisper) — nutzt torch/MPS auf Apple Silicon.
# Modelle liegen unter ~/.cache/whisper/{size}.pt.
import whisper
try:
    import torch
    _HAS_TORCH = True
except Exception:
    _HAS_TORCH = False
import pyperclip
from pynput import keyboard
from pynput.keyboard import Key, KeyCode, Controller

# =====================================================================
#  macOS 26+ Fix: pynput ruft TSMGetInputSourceProperty aus seinem
#  Listener-Thread auf (keycode_context() generator in pynput/_util/
#  darwin.py:140). macOS 26 enforced main-thread-only für TSM/Carbon —
#  Listener-Thread crasht mit dispatch_assert_queue_fail.
#
#  Fix: wir rufen keycode_context() genau einmal auf dem Main-Thread beim
#  Import auf und cachen das Ergebnis. Dann monkey-patchen wir pynput,
#  sodass der Listener-Thread die gecachte Version bekommt. Keyboard-
#  Layout-Wechsel während der Session werden dadurch ignoriert — für
#  einen Single-Layout-Hotkey-Workflow egal.
# =====================================================================
import contextlib as _contextlib
import pynput._util.darwin as _pynput_darwin
import pynput.keyboard._darwin as _pynput_keyboard_darwin

_cached_keycode_ctx = None
try:
    with _pynput_darwin.keycode_context() as _ctx:
        _cached_keycode_ctx = _ctx
except Exception:
    # Falls Prewarm scheitert, lassen wir pynput im Originalzustand
    # (crasht dann wie gewohnt — zumindest sehen wir den Fehler im Log).
    pass


@_contextlib.contextmanager
def _cached_keycode_context():
    yield _cached_keycode_ctx


if _cached_keycode_ctx is not None:
    _pynput_darwin.keycode_context = _cached_keycode_context
    # pynput.keyboard._darwin hat keycode_context per "from ... import"
    # gebunden — daher auch dort ersetzen.
    _pynput_keyboard_darwin.keycode_context = _cached_keycode_context

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

# Dock-Icon-Handling: ab v4 hat die App ein Dock-Icon (kein LSUIElement im
# Info.plist mehr). Ein Klick aufs Dock-Symbol soll das Hauptfenster öffnen
# - das wird via QEvent.ApplicationActivate in IQspeakrApp gemacht. Der
# NSStatusItem-Tray läuft als eigener Subprocess und ist von der
# Activation-Policy unabhängig.
from PySide6.QtCore import QEvent

# --- Pfade ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_CONFIG = os.path.join(APP_DIR, "config.json")
USER_DIR = str(Path.home() / "IQspeakr")
CONFIG_PATH = os.path.join(USER_DIR, "config.json")
APP_ICON_PATH = os.path.join(APP_DIR, "IQspeakr.icns")

# Hauptfenster-State (History + Dashboard) liegt im selben User-Dir wie
# die Config — Mac hat keinen %APPDATA%-Äquivalent, aber ~/IQspeakr ist
# genau der "user data"-Spot, den die App eh schon nutzt.
HISTORY_PATH = os.path.join(USER_DIR, "history.json")
HISTORY_MAX = 10
STATS_DB_PATH = os.path.join(USER_DIR, "stats.db")
# Wörterbuch: korrekte Schreibweisen für Eigennamen, die Whisper falsch
# versteht (z.B. "IQspeakr" -> "Ich Sprecher"). Wird vor Ollama-Cleanup
# angewendet.
DICTIONARY_PATH = os.path.join(USER_DIR, "dictionary.json")

os.makedirs(USER_DIR, exist_ok=True)
if not os.path.exists(CONFIG_PATH) and os.path.exists(BUNDLE_CONFIG):
    import shutil
    shutil.copy2(BUNDLE_CONFIG, CONFIG_PATH)

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_BASE = "http://localhost:11434"
# v5: stiller In-App-Ollama-Install. Wir laden das pure CLI-Binary
# (kein Tray, kein Dock, kein .app) aus dem offiziellen GitHub-Release
# und legen es in OLLAMA_LOCAL_BIN ab. /latest/download redirected
# GitHub automatisch auf die aktuellste Version, daher kein Versions-
# schwanz hier hardgecodet.
OLLAMA_DARWIN_TGZ_URL = (
    "https://github.com/ollama/ollama/releases/latest/download/ollama-darwin.tgz"
)
OLLAMA_LOCAL_BIN_DIR = os.path.join(USER_DIR, "bin")
OLLAMA_LOCAL_BIN = os.path.join(OLLAMA_LOCAL_BIN_DIR, "ollama")
SAMPLE_RATE = 16000

# =====================================================================
#  Theme-Tokens. Eine Stelle für alle Farben - sonst driftet das auseinander.
#  Identisch mit windows_v2/app.py, damit Mac und Windows gleich aussehen.
# =====================================================================

THEME_BG            = "#16181D"
THEME_BG_SIDEBAR    = "#0F1115"
THEME_BG_CARD       = "#1B1E25"
THEME_BG_INPUT      = "#1F232C"
THEME_BG_HOVER      = "rgba(255, 255, 255, 0.05)"
THEME_BORDER        = "#2A2D35"
THEME_BORDER_HOVER  = "#3A3F4A"
THEME_BORDER_SOFT   = "rgba(255, 255, 255, 0.06)"
THEME_TEXT          = "#F1F5F9"
THEME_TEXT_SECONDARY = "#CBD5E1"
THEME_TEXT_MUTED    = "#8C92A0"
THEME_ACCENT        = "#6366F1"
THEME_ACCENT_HOVER  = "#7B7DF5"
THEME_ACCENT_SOFT   = "rgba(99, 102, 241, 0.18)"
THEME_DANGER        = "#EF4444"
THEME_SUCCESS       = "#22C55E"
THEME_WARNING       = "#F59E0B"


def apply_app_theme(qapp):
    """Setzt System-Font + globale QSS auf die QApplication. Wird einmal in
    main() aufgerufen, danach erbt jedes Widget davon. Lokale setStyleSheet()-
    Aufrufe in einzelnen Klassen ergänzen / spezialisieren."""
    # SF Pro Display ist macOS-Standard ab Big Sur; auf älteren Macs fällt
    # Qt automatisch auf SF Pro Text bzw. Helvetica Neue zurück.
    from PySide6.QtGui import QFont as _QF
    font = _QF(".AppleSystemUIFont")  # systemFont alias
    font.setPointSizeF(13.0)
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
#  Lucide-Icons inline (lucide.dev, ISC).
# =====================================================================

_LUCIDE_HOME = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>"""
_LUCIDE_TYPE = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 7 4 4 20 4 20 7"/><line x1="9" x2="15" y1="20" y2="20"/><line x1="12" x2="12" y1="4" y2="20"/></svg>"""
_LUCIDE_SETTINGS = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>"""
_LUCIDE_COPY = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>"""
_LUCIDE_CHECK = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>"""
_LUCIDE_BAR_CHART = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>"""

_LUCIDE_MIC = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>"""

_LUCIDE_INFO = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>"""

# Wörterbuch (Lucide "book-open"): aufgeschlagenes Buch — passt sowohl
# semantisch (Glossar/Begriffe) als auch visuell zur Sidebar-Reihe.
_LUCIDE_BOOK = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>"""
_LUCIDE_PLUS = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>"""
_LUCIDE_PENCIL = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/></svg>"""
_LUCIDE_TRASH = """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>"""


def _make_app_logo_pixmap(size=28):
    """Indigo-Quadrat (abgerundet) + weißes Mikrofon-Symbol als Pixmap.
    Wird im Sidebar-Header verwendet — die `.icns`-Datei brauchen wir hier
    nicht, das Logo soll konsistent zum Theme-Akzent rendern."""
    from PySide6.QtSvg import QSvgRenderer
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    # Indigo-Quadrat (Akzentfarbe)
    p.setBrush(QColor(THEME_ACCENT))
    p.setPen(Qt.NoPen)
    radius = max(4, size // 5)
    p.drawRoundedRect(0, 0, size, size, radius, radius)
    # Weißes Mikrofon-Symbol mittig - 60% des Quadrats
    icon_size = int(size * 0.62)
    icon_x = (size - icon_size) // 2
    icon_y = (size - icon_size) // 2
    svg = _LUCIDE_MIC.replace("currentColor", "#FFFFFF")
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    from PySide6.QtCore import QRectF as _QRectF
    renderer.render(p, _QRectF(icon_x, icon_y, icon_size, icon_size))
    p.end()
    return pm


def _lucide_icon(svg_template, size=18, color=None):
    """Rendert einen Lucide-SVG-Template-String zur QIcon. `color` ersetzt
    den `currentColor`-Stroke."""
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QByteArray as _QBA
    color = color or THEME_TEXT_SECONDARY
    svg = svg_template.replace("currentColor", color)
    renderer = QSvgRenderer(_QBA(svg.encode("utf-8")))
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
    """Always-on-top Pill mit 7 Live-Waveform-Balken unten-mittig.
    Audio-Thread schreibt nur atomische Python-Attribute (thread-safe in
    CPython), der QTimer im Main-Thread liest sie - keine Cross-Thread
    Qt-Signals aus C-Callbacks nötig (vermeidet Stack-Races unter
    Whisper/Torch-Parallelbetrieb)."""

    BAR_COUNT = 7
    W = 180
    H = 36
    # Overlay ist IMMER sichtbar. Im Idle dezent (niedrige Opacity),
    # während Aufnahme kräftig. Der User wollte kein Auftauchen/
    # Verschwinden-Blitz, sondern eine ruhige dauerhafte Anwesenheit.
    IDLE_ALPHA = 0.22       # dezent, aber sichtbar
    ACTIVE_ALPHA = 0.92     # deutlich, während Aufnahme
    # Abstand zum unteren Bildschirmrand. availableGeometry() respektiert
    # Dock+Menubar — 16px darüber sind genug, sonst schwebt die Pille zu hoch.
    MARGIN_BOTTOM = 16

    def __init__(self, enabled=True):
        # Qt.Tool + WindowStaysOnTop + FramelessWindowHint ist die robuste
        # Mac-Kombi für ein Overlay-Widget. WindowDoesNotAcceptFocus
        # sorgt dafür, dass das aktive Fenster des Users nicht deaktiviert
        # wird wenn das Overlay erscheint.
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.Tool | Qt.WindowDoesNotAcceptFocus,
        )
        self.enabled = enabled
        self._levels = [0.0] * self.BAR_COUNT
        self._active = False
        self._current_alpha = 0.0
        self._target_alpha = self.IDLE_ALPHA
        self._idle_phase = 0.0  # für das dezente Idle-Atmen

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
        # Timer läuft nur während Aufnahme — im Idle nichts zu animieren.

        # Initial unsichtbar. Overlay erscheint erst bei set_recording(True)
        # und verschwindet wieder bei set_recording(False).
        self.setWindowOpacity(0.0)
        self._nswin_configured = False

    def _apply_macos_overlay_level(self):
        try:
            import objc
            from AppKit import (
                NSStatusWindowLevel,
                NSWindowCollectionBehaviorCanJoinAllSpaces,
                NSWindowCollectionBehaviorStationary,
                NSWindowCollectionBehaviorFullScreenAuxiliary,
                NSWindowCollectionBehaviorIgnoresCycle,
            )
            # winId() liefert unter Qt/macOS einen Pointer auf das NSView.
            # Via objc.objc_object(c_void_p=...) zurück in ein Python-Objekt
            # wandeln und .window() aufrufen.
            view = objc.objc_object(c_void_p=int(self.winId()))
            nswin = view.window()
            if nswin is None:
                log.warning("PillOverlay: NSWindow nicht gefunden")
                return
            nswin.setLevel_(NSStatusWindowLevel)
            nswin.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary
                | NSWindowCollectionBehaviorIgnoresCycle
            )
            # Nicht aktivieren beim Zeigen — vermeidet Fokus-Klau.
            nswin.setHidesOnDeactivate_(False)
            log.info("PillOverlay: NSWindow auf Status-Level (always-on-top)")
        except Exception as e:
            log.warning(f"PillOverlay: NSWindow-Level konnte nicht gesetzt werden: {e}")

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
        sonst versteckt. Kein Idle-Zustand mehr."""
        if not self.enabled:
            return
        self._active = bool(on)
        if on:
            self._move_to_primary_screen()
            self._current_alpha = 0.0
            self._target_alpha = self.ACTIVE_ALPHA
            self.setWindowOpacity(0.0)
            self.show()
            if not self._nswin_configured:
                # NSWindow-Level erst nach erstem show() setzen —
                # vorher existiert das NSWindow noch nicht.
                self._apply_macos_overlay_level()
                self._nswin_configured = True
            self._timer.start()
        else:
            self._levels = [0.0] * self.BAR_COUNT
            self._timer.stop()
            self.setWindowOpacity(0.0)
            self.hide()


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

# Mac-Symbole: ⌃ ctrl, ⇧ shift, ⌥ alt/option, ⌘ cmd.
# Kein Leerzeichen zwischen Symbolen (ctrl+shift -> ⌃⇧, cmd+shift+r -> ⌘⇧R).
HOTKEY_DISPLAY = {
    "ctrl": "⌃", "control": "⌃",
    "shift": "⇧",
    "alt": "⌥", "option": "⌥",
    "cmd": "⌘", "command": "⌘", "win": "⌘",
    "space": "Leertaste",
    "enter": "Eingabe",
    "tab": "Tab",
}

# Qt-Key -> interner Hotkey-String (für Custom-Hotkey-Recorder)
_QT_MOD_TO_NAME = {
    Qt.Key_Control: "ctrl",
    Qt.Key_Shift:   "shift",
    Qt.Key_Alt:     "alt",
    Qt.Key_Meta:    "cmd",
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
    has_symbol = False
    for part in parts:
        part = part.strip()
        if part in HOTKEY_DISPLAY:
            display_parts.append(HOTKEY_DISPLAY[part])
            # Unicode-Modifier-Symbole sind single-char; "Leertaste"/"Eingabe"
            # sind Worte. Der Join hängt davon ab, ob nur Symbole im Spiel
            # sind.
            if part in ("ctrl", "control", "shift", "alt", "option",
                        "cmd", "command", "win"):
                has_symbol = True
        else:
            display_parts.append(part.upper())
    # Wenn die Kombi nur aus Modifiern besteht oder Modifier+Einzelbuchstabe:
    # Symbole zusammenziehen (⌘⇧R). Sobald Wort-Keys (Leertaste/Eingabe)
    # dabei sind, mit "+" trennen damit's lesbar bleibt.
    has_wordkey = any(p.strip() in ("space", "enter", "tab") for p in parts)
    if has_symbol and not has_wordkey:
        return "".join(display_parts)
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
    # openai-whisper cached Modelle unter ~/.cache/whisper/{size}.pt.
    return os.path.isfile(os.path.expanduser(f"~/.cache/whisper/{size}.pt"))


# --- Config ---
DEFAULT_CONFIG = {
    # Default cross-platform konsistent mit Windows.
    "hotkey": "ctrl+shift",
    "whisper_model": "base",
    "ollama_model": "llama3.2",
    "cleanup_enabled": True,
    "language": "de",
    "overlay_enabled": True,
    # Master-Switch für die Ollama-Integration. True = Default (App
    # versucht Ollama zu detecten und nutzt es für Cleanup). False = User
    # hat in den Settings explizit "Ollama deaktivieren" geklickt.
    "ollama_disabled": False,
    # Fenster-Geometrie wird beim Resize debounced persistiert. Default
    # 1280x900 sorgt dafür, dass die Style-Cards in 4er-Reihe sichtbar
    # sind und die Settings-Card nicht von der Heatmap zerquetscht wird.
    "window_width": 1280,
    "window_height": 900,
    # Cleanup-Prompt-Stil: formal | locker | sehr_locker | custom.
    # StyleView in der App schreibt das Feld; Default = locker (= das
    # Verhalten der Vorgänger-Version).
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



CLEANUP_PROMPT_LOCKER = """Du bereinigst gesprochene Sprache minimal-invasiv. WICHTIG: Du DARFST den Text NICHT umformulieren oder paraphrasieren. Der Sprecher soll seinen eigenen Stil wiedererkennen.

ERLAUBT:
- Füllwörter entfernen (ähm, äh, also, sozusagen, halt, quasi, irgendwie, eben, ja, nun)
- Wortdoppelungen und Stotterer entfernen (z.B. "ich ich habe" -> "ich habe")
- Satzzeichen und Großschreibung korrigieren
- Offensichtliche Grammatikfehler korrigieren (z.B. falsche Artikel, Kasus)
- Kleine Satzumstellungen NUR wenn grammatikalisch notwendig

VERBOTEN:
- Wörter durch Synonyme ersetzen
- Sätze neu formulieren oder glätten
- Inhalt straffen oder zusammenfassen
- Stil verändern (umgangssprachlich -> schriftsprachlich)
- Eigene Wörter hinzufügen

Antworte NUR mit dem bereinigten Text, ohne Erklärungen, ohne Anführungszeichen.

Text: {text}"""

CLEANUP_PROMPT_FORMAL = """Du bereinigst gesprochene Sprache und überführst sie in geschriebenes, förmliches Deutsch. Der Inhalt bleibt vollständig erhalten - nur Form und Register werden angehoben.

ERLAUBT:
- Füllwörter, Wortdoppelungen und Stotterer entfernen
- Satzzeichen, Großschreibung und Grammatik korrigieren
- Umgangssprache durch schriftsprachliche Äquivalente ersetzen (z.B. "halt" -> entfernen, "kriegen" -> "erhalten", "ne" -> "eine")
- Sätze umstellen, wenn der Schriftstil das verlangt
- Höflichkeitsformen verwenden, wenn aus dem Kontext klar erkennbar
- Verkürzungen ausschreiben (z.B. "geht's" -> "geht es", "ist's" -> "ist es")

VERBOTEN:
- Inhalt streichen oder zusammenfassen
- Eigene Aussagen hinzufügen
- Inhaltliche Aussage verändern

Antworte NUR mit dem bereinigten Text, ohne Erklärungen, ohne Anführungszeichen.

Text: {text}"""

CLEANUP_PROMPT_SEHR_LOCKER = """Du entfernst nur Spracharten der Pause aus diktierter Sprache. Sonst NICHTS. Der Sprecher will seinen Originaltext exakt 1:1 zurück, nur ohne Stotterer.

ERLAUBT (und nur das):
- Reine Fülllaute entfernen: "ähm", "äh", "öh", "mhm"
- Direkte Wortdoppelungen entfernen, wenn klar ein Stotterer ist (z.B. "ich ich habe" -> "ich habe", aber NICHT "sehr sehr gut")
- Offensichtliche Satzzeichen am Satzende setzen (Punkt, Fragezeichen)

VERBOTEN:
- Grammatik korrigieren
- Umgangssprache ändern
- Wortdoppelungen entfernen, die zur Betonung dienen
- Groß-/Kleinschreibung anders setzen als im Original (außer am Satzanfang)
- Satzumstellungen
- Synonyme einsetzen
- Füllwörter wie "halt", "also", "quasi" entfernen - die sind Stil

Antworte NUR mit dem unveränderten Text minus Fülllaute, ohne Erklärungen, ohne Anführungszeichen.

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
            os.makedirs(USER_DIR, exist_ok=True)
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
        os.makedirs(USER_DIR, exist_ok=True)
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
            log.info(f"StatsStore: {n} Legacy-History-Eintraege migriert")
        return n


# =====================================================================
#  Wörterbuch: Eigennamen-Korrektur, die *immer* angewendet wird
#  (vor Ollama-Cleanup). Persistiert als JSON-Liste in dictionary.json.
#  Datenmodell pro Eintrag:
#      {"correct": "IQspeakr", "variants": ["ich-sprecher", "ix speaker"]}
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
            os.makedirs(USER_DIR, exist_ok=True)
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
#  Ollama-Manager (macOS-Variante): State-Maschine + Worker für
#  Detection / User-getriebenen Install / Pull.
#  Auf dem Mac wird Ollama nicht silent installiert - der User lädt die
#  Ollama.app von ollama.com und zieht sie selbst nach /Applications.
#  Wir öffnen ihm die Download-URL und warten dann auf den Service.
# =====================================================================

# State-Konstanten - kein Enum, damit die Werte direkt in die Config
# / Logs / UI-Strings wandern können ohne .name/.value-Indirektion.
OLLAMA_NOT_INSTALLED      = "not_installed"
# v5: stille Installation läuft (Tarball-Download + Extract). Ersetzt
# das v4-WAITING_FOR_USER, das auf einen User-Click in der Ollama.app
# wartete. WAITING_FOR_USER ist nicht mehr erreichbar, der Konstanten-
# Name bleibt aber drin, falls externe Konsumenten ihn referenzieren.
OLLAMA_INSTALLING         = "installing"
OLLAMA_WAITING_FOR_USER   = "waiting_for_user"
# Ollama-Service ist erreichbar, aber das Wunsch-Modell ist noch nicht
# heruntergeladen. Der User muss den Pull manuell auslösen — sonst weiß
# er nicht, dass jetzt ein 2-4 GB Download startet.
OLLAMA_NEEDS_MODEL        = "needs_model"
OLLAMA_PULLING            = "pulling_model"
OLLAMA_READY              = "ready"
OLLAMA_ERROR              = "error"
# User hat die ganze Integration ausgeschaltet (Master-Switch). Persistiert
# in config.ollama_disabled. Im DISABLED-State wird kein Service gepingt,
# Cleanup läuft nicht, Style ist gesperrt.
OLLAMA_DISABLED           = "disabled"

# Modell-Optionen für den Setup-Dropdown. Reihenfolge = UI-Reihenfolge.
OLLAMA_MODEL_OPTIONS = [
    ("llama3.2",  "llama3.2 (3B) - klein und schnell, Standard"),
    ("llama3.1",  "llama3.1 (8B) - bessere Qualität, mehr RAM"),
    ("mistral",   "mistral (7B) - gut für Deutsch und Englisch"),
    ("gemma2",    "gemma2 (9B) - Google, solide Qualität"),
    ("phi3",      "phi3 (3.8B) - Microsoft, kompakt und gut"),
]


class _PullCancelled(Exception):
    """Interne Exception um einen laufenden Modell-Pull abzubrechen."""
    pass


class OllamaManager(QObject):
    """Steuert Detection / stillen Install / Pull des Ollama-Service.

    v5-Änderung gegenüber v4: kein Browser-Redirect mehr. Beim Klick auf
    "Ollama installieren" zieht der Manager das offizielle CLI-Binary
    direkt aus dem GitHub-Release nach OLLAMA_LOCAL_BIN und startet
    `ollama serve` als detached Subprocess. Kein Tray, kein Dock-Icon,
    keine Ollama.app — der User sieht Ollama nirgendwo. Beim App-Quit
    wird der gespawnte Service via shutdown() beendet.

    Wenn auf 11434 schon was anderes lauscht (z.B. ein Brew-Service
    oder eine manuell installierte Ollama.app), wird DAS genutzt, ohne
    eine zusätzliche Instanz zu starten.

    Alle Worker laufen in eigenen Threads, GUI-Updates ausschließlich
    über Qt-Signals (queued, automatisch im Main-Thread)."""

    state_changed = Signal(str)             # neuer State-String
    pull_progress = Signal(int, str)        # percent 0-100, Status-Text
    install_progress = Signal(int, str)     # v5: Tarball-Download/Extract
    error_message = Signal(str)             # User-lesbare Fehlermeldung

    HTTP_TIMEOUT = 5.0
    SERVICE_WAIT_SECONDS = 60     # Watch-Worker wartet bis 30 min, aber
                                  # einzelne Ping-Schleifen orientieren
                                  # sich an diesem Wert.
    USER_INSTALL_TIMEOUT = 30 * 60   # Legacy-Konstante (v4)
    USER_INSTALL_POLL = 5            # Sekunden zwischen Ping-Versuchen
    SERVE_BOOT_TIMEOUT = 30          # Sekunden bis `ollama serve` antwortet

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = OLLAMA_NOT_INSTALLED
        self._busy = False  # serialisiert Install/Pull
        self._lock = threading.Lock()
        # Wird vom User gesetzt um eine laufende Installation/Wait
        # abzubrechen. Worker prüft das Flag in der Schleife.
        self._cancel_install = False
        # Wird vom User gesetzt um einen laufenden Modell-Pull
        # abzubrechen. Pull-Worker prüft das Flag im Chunk-Read-Loop.
        self._cancel_pull = False
        # v5: Handle auf den von uns gespawnten `ollama serve`-Prozess.
        # None solange wir keinen eigenen Service starten (z.B. wenn
        # bereits Brew-Ollama läuft). Wird in shutdown() abgeräumt.
        self._serve_proc = None

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
        eingestellte Modell - wenn vorhanden + Service läuft -> READY.

        Wenn der User die Integration in den Settings deaktiviert hat
        (state == OLLAMA_DISABLED), pingen wir gar nicht erst - sonst
        überschreibt der Worker den User-Wunsch."""
        if self._state == OLLAMA_DISABLED:
            return
        threading.Thread(
            target=self._refresh_worker,
            args=(current_model,),
            daemon=True,
        ).start()

    def disable_integration(self):
        """User-Klick: Ollama-Integration aus. Etwaige laufende Worker
        werden via Cancel-Flags beendet, dann setzen wir den finalen
        State auf DISABLED. config.ollama_disabled wird vom Caller
        persistiert (wir können hier nicht config schreiben, der
        Manager kennt die config nicht)."""
        log.info("OllamaManager: User hat Integration deaktiviert")
        self._cancel_install = True
        self._cancel_pull = True
        self._set_state(OLLAMA_DISABLED)

    def enable_integration(self, current_model=None):
        """User-Klick: Ollama-Integration wieder an. Zurück zu Detection-
        Flow über refresh_state."""
        log.info("OllamaManager: User hat Integration aktiviert")
        # Direkt aus DISABLED rausspringen, sonst skippt refresh_state.
        self._state = OLLAMA_NOT_INSTALLED
        self.state_changed.emit(self._state)
        self.refresh_state(current_model)

    def _refresh_worker(self, current_model):
        ok, models = self._ping_service()
        if not ok:
            # v5: wenn unser lokales Binary schon installiert ist (vom
            # vorigen App-Run) aber `ollama serve` aktuell nicht läuft,
            # spawnen wir es stillschweigend nach. Erst danach noch ein
            # Ping. So kommt der User nach einem Neustart nicht erneut
            # in den NOT_INSTALLED-Knopf-Flow.
            if os.path.exists(OLLAMA_LOCAL_BIN):
                log.info("OllamaManager: lokales Binary vorhanden, starte serve")
                if self._spawn_serve() and self._wait_for_service(self.SERVE_BOOT_TIMEOUT):
                    ok, models = self._ping_service()
            if not ok:
                self._set_state(OLLAMA_NOT_INSTALLED)
                return
        # Service läuft. Prüfe ob das eingestellte Modell schon gepullt
        # ist - wenn nicht, NEEDS_MODEL (User triggert Pull manuell), sonst
        # READY. Vermeidet überraschende 2-4 GB Downloads beim App-Start.
        if current_model:
            prefixes = [current_model, current_model + ":"]
            if any(any(m.startswith(p) for p in prefixes) for m in models):
                self._set_state(OLLAMA_READY)
                return
            self._set_state(OLLAMA_NEEDS_MODEL)
            return
        # Kein Modell-Name übergeben - default optimistisch READY.
        self._set_state(OLLAMA_READY)

    def has_model(self, name):
        """Prüft synchron ob ein Modell schon gepullt ist."""
        ok, models = self._ping_service()
        if not ok:
            return False
        # Modelle können mit ":latest" o.ae. ankommen.
        prefixes = [name, name + ":"]
        return any(any(m.startswith(p) for p in prefixes) for m in models)

    # --- Install (v5: stiller In-App-Download) ---
    def install(self, model_name):
        """v5: lädt das Ollama-CLI-Binary still aus dem GitHub-Release,
        extrahiert es nach OLLAMA_LOCAL_BIN, startet `ollama serve` als
        detached Subprocess. Kein Browser, kein Tray, kein Dock-Icon.
        Modell wird NICHT automatisch gezogen — start_pull() ist ein
        separater User-Schritt."""
        with self._lock:
            if self._busy:
                self.error_message.emit("Eine andere Aktion läuft bereits.")
                return
            self._busy = True
            self._cancel_install = False
        self._set_state(OLLAMA_INSTALLING)
        threading.Thread(
            target=self._silent_install_worker,
            args=(model_name,),
            daemon=True,
        ).start()

    def cancel_install(self):
        """Bricht den stillen Install ab. Worker prüft das Flag in der
        Download-Schleife und im Service-Wait und kehrt zu NOT_INSTALLED
        zurück."""
        log.info("OllamaManager: User hat Install abgebrochen")
        self._cancel_install = True

    def _silent_install_worker(self, model_name):
        """Worker für den stillen Install-Flow. Drei Phasen:

           1. Tarball downloaden -> install_progress(percent, "X / Y MB")
           2. ollama-Binary entpacken -> install_progress(-1, "Extrahiere…")
           3. `ollama serve` spawnen + auf API warten

        Bei Cancel/Fehler zurück zu NOT_INSTALLED, _busy wird im finally
        zurückgesetzt damit ein erneuter Klick wieder durchkommt.
        """
        tmp_path = None
        try:
            os.makedirs(OLLAMA_LOCAL_BIN_DIR, exist_ok=True)
            tmp_path = os.path.join(
                OLLAMA_LOCAL_BIN_DIR, ".ollama-darwin.tgz.part",
            )

            # Phase 1: Download
            self.install_progress.emit(0, "Verbinde…")
            self._download_tarball(tmp_path)
            if self._cancel_install:
                self._set_state(OLLAMA_NOT_INSTALLED)
                return

            # Phase 2: Extract
            self.install_progress.emit(-1, "Entpacke…")
            self._extract_ollama_binary(tmp_path, OLLAMA_LOCAL_BIN)
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            tmp_path = None

            if self._cancel_install:
                self._set_state(OLLAMA_NOT_INSTALLED)
                return

            # Phase 3: Spawn serve + warten
            self.install_progress.emit(-1, "Starte Ollama-Service…")
            if not self._spawn_serve():
                raise RuntimeError("Konnte ollama serve nicht starten.")
            if not self._wait_for_service(self.SERVE_BOOT_TIMEOUT):
                raise RuntimeError(
                    "Ollama-Service antwortet nicht — möglicherweise "
                    "blockiert eine Firewall den lokalen Port 11434."
                )

            # Service läuft. Modell-Status prüfen.
            ok, models = self._ping_service(timeout=2.0)
            if ok and model_name:
                prefixes = [model_name, model_name + ":"]
                if any(any(m.startswith(p) for p in prefixes) for m in models):
                    log.info(
                        f"OllamaManager: Install fertig, Modell "
                        f"'{model_name}' schon da -> READY"
                    )
                    self._set_state(OLLAMA_READY)
                    return
            log.info("OllamaManager: Install fertig -> NEEDS_MODEL")
            self._set_state(OLLAMA_NEEDS_MODEL)
        except _PullCancelled:
            # via cancel_install ausgelöst (Download-Loop wirft das)
            log.info("OllamaManager: Install via Cancel beendet")
            self._set_state(OLLAMA_NOT_INSTALLED)
        except Exception as e:
            log.exception("OllamaManager: silent install fehlgeschlagen")
            self.error_message.emit(str(e))
            self._set_state(OLLAMA_ERROR)
            # Halb-fertige Reste aufräumen, damit ein Retry sauber startet.
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
        finally:
            with self._lock:
                self._busy = False
                self._cancel_install = False

    def _download_tarball(self, dest_path):
        """Lädt OLLAMA_DARWIN_TGZ_URL nach dest_path mit Chunk-Loop und
        Progress-Signal. Wirft _PullCancelled wenn _cancel_install gesetzt
        wurde — gleicher Cancel-Pfad wie beim Modell-Pull."""
        req = urllib.request.Request(
            OLLAMA_DARWIN_TGZ_URL,
            headers={"User-Agent": "IQspeakr-Installer"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            try:
                total = int(resp.headers.get("Content-Length", "0"))
            except Exception:
                total = 0
            downloaded = 0
            with open(dest_path, "wb") as f:
                while True:
                    if self._cancel_install:
                        raise _PullCancelled()
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(downloaded * 100 / total)
                        self.install_progress.emit(
                            pct,
                            f"{downloaded // (1024*1024)} / "
                            f"{total // (1024*1024)} MB",
                        )
                    else:
                        self.install_progress.emit(
                            -1, f"{downloaded // (1024*1024)} MB",
                        )
        log.info(
            f"OllamaManager: Tarball heruntergeladen ({downloaded} bytes) "
            f"-> {dest_path}"
        )

    def _extract_ollama_binary(self, tarball_path, dest_path):
        """Sucht im tar.gz das `ollama`-Binary (in egal welchem Unter-
        verzeichnis) und schreibt es atomar nach dest_path mit
        ausführbaren Rechten. Wirft RuntimeError wenn nichts gefunden."""
        with tarfile.open(tarball_path, "r:gz") as tf:
            target_member = None
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                if os.path.basename(m.name) == "ollama":
                    target_member = m
                    break
            if target_member is None:
                raise RuntimeError(
                    "Kein 'ollama'-Binary im Tarball gefunden — "
                    "Format hat sich geändert?"
                )
            tmp_dest = dest_path + ".part"
            src = tf.extractfile(target_member)
            if src is None:
                raise RuntimeError("Tarball-Eintrag nicht lesbar.")
            with open(tmp_dest, "wb") as out:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            os.chmod(tmp_dest, 0o755)
            # Atomic-Rename, so dass dest_path nie halb-geschrieben ist.
            os.replace(tmp_dest, dest_path)
        log.info(f"OllamaManager: ollama-Binary entpackt -> {dest_path}")

    def _spawn_serve(self):
        """Startet `ollama serve` als detached Background-Prozess. Logs
        landen in ~/IQspeakr/ollama-serve.log. Idempotent: wenn unser
        Prozess noch läuft, return True ohne erneut zu spawnen."""
        if self._serve_proc is not None and self._serve_proc.poll() is None:
            return True
        if not os.path.exists(OLLAMA_LOCAL_BIN):
            log.warning(
                f"OllamaManager: _spawn_serve ohne Binary unter "
                f"{OLLAMA_LOCAL_BIN}"
            )
            return False
        try:
            log_path = os.path.join(USER_DIR, "ollama-serve.log")
            log_f = open(log_path, "ab")
            self._serve_proc = subprocess.Popen(
                [OLLAMA_LOCAL_BIN, "serve"],
                stdout=log_f, stderr=log_f, stdin=subprocess.DEVNULL,
                # Eigene Session => SIGINT an IQspeakr trifft Ollama nicht.
                start_new_session=True,
            )
            log.info(
                f"OllamaManager: ollama serve gestartet (PID "
                f"{self._serve_proc.pid}, Log {log_path})"
            )
            return True
        except Exception as e:
            log.error(f"OllamaManager: spawn serve fehlgeschlagen: {e}")
            self._serve_proc = None
            return False

    def _wait_for_service(self, timeout):
        """Pingt /api/tags bis OK oder Timeout. Return True wenn Service
        antwortet. Cancel-flag bricht früh ab."""
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if self._cancel_install:
                return False
            ok, _ = self._ping_service(timeout=1.0)
            if ok:
                return True
            _time.sleep(0.5)
        return False

    def shutdown(self):
        """Beendet den von uns gespawnten ollama serve Prozess. Wird beim
        App-Quit aufgerufen. Idempotent — wenn nichts läuft, no-op."""
        proc = self._serve_proc
        self._serve_proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            log.info("OllamaManager: ollama serve gestoppt")
        except Exception as e:
            log.warning(f"OllamaManager: shutdown failed: {e}")

    def start_pull(self, model_name):
        """Public: User-getriggerter Modell-Pull. Wechselt den State auf
        PULLING, zieht das Modell, am Ende READY."""
        with self._lock:
            if self._busy:
                self.error_message.emit("Eine andere Aktion läuft bereits.")
                return
            self._busy = True
            self._cancel_pull = False
        threading.Thread(
            target=self._pull_only_worker,
            args=(model_name,),
            daemon=True,
        ).start()

    def cancel_pull(self):
        """Bricht einen laufenden Modell-Pull ab. Der Worker prüft das Flag
        im Chunk-Read-Loop und wirft eine Exception, die zu NEEDS_MODEL
        zurückführt (Service läuft, Modell nur halb da)."""
        log.info("OllamaManager: User hat Pull abgebrochen")
        self._cancel_pull = True

    def _pull_only_worker(self, model_name):
        try:
            self._set_state(OLLAMA_PULLING)
            self._pull(model_name)
            self._set_state(OLLAMA_READY)
        except _PullCancelled:
            log.info("OllamaManager: Pull abgebrochen, zurueck zu NEEDS_MODEL")
            self._set_state(OLLAMA_NEEDS_MODEL)
        except Exception as e:
            log.exception("OllamaManager: pull fehlgeschlagen")
            self.error_message.emit(str(e))
            self._set_state(OLLAMA_ERROR)
        finally:
            with self._lock:
                self._busy = False
                self._cancel_pull = False

    def _pull(self, model_name):
        """POST /api/pull mit stream=true. Parst JSONL-Stream und emittet
        pull_progress(percent, status). Wirft _PullCancelled wenn der User
        cancel_pull() aufgerufen hat."""
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
                if self._cancel_pull:
                    # Verbindung schließt sich automatisch beim Verlassen
                    # des with-Blocks - urllib gibt die TCP-Connection frei,
                    # Ollama bricht den Server-seitigen Stream ab.
                    raise _PullCancelled()
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

    # pull_model() ist Alias auf start_pull() - Aufrufer in der SettingsView
    # nutzt diesen Namen wenn der User das Modell wechselt während Ollama
    # schon läuft.
    pull_model = start_pull


# --- Tray-Icon-Helfer (Qt rendert Emoji als PNG statt Plain-Kreis) ---

# Mikrofon/Rec/Busy-Emoji aus der System-Emoji-Font in ein PNG rendern.
# Grund: die alte Mac-Version zeigte 🎤 als Menubar-Title; QSystemTrayIcon
# braucht aber ein Bild, kein Text. Qt kann Emojis über QFont rendern —
# sieht in der Menubar fast identisch aus zu rumps' Text-Title.
from PySide6.QtGui import QFont
from PySide6.QtCore import QRectF

_ICON_EMOJIS = {
    "ready": "🎤",
    "rec":   "🔴",
    "busy":  "⏳",
}


# =====================================================================
#  NativeStatusBar — Ersatz für QSystemTrayIcon.
#
#  Qts QSystemTrayIcon-Cocoa-Backend rendert auf macOS 26 das Icon nicht
#  zuverlässig (Qt meldet visible=True, trotzdem bleibt die Menubar
#  leer). Wir umgehen Qt komplett und bauen direkt auf NSStatusBar +
#  NSStatusItem + NSMenu — derselbe Mechanismus den rumps in v1 nutzt
#  und der sicher funktioniert.
#
#  Menü-Klicks werden an die bestehenden QAction-Objekte (aus _build_menu)
#  weitergereicht; das Gros der Qt-Struktur bleibt erhalten.
# =====================================================================
_NATIVE_ICON_TITLES = {"ready": "🎤", "rec": "🔴", "busy": "⏳"}


try:
    from Foundation import NSObject
    import objc as _objc

    class _NativeMenuTarget(NSObject):
        """NSObject-Target für NSMenuItem-Klicks. Ruft ein Python-
        Callable auf. MUSS NSObject sein, sonst akzeptiert AppKit es
        nicht als target."""
        def initWithCallback_(self, cb):
            self = _objc.super(_NativeMenuTarget, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def clicked_(self, sender):
            try:
                self._cb()
            except Exception as e:
                log.exception(f"NativeMenu-Callback fehlgeschlagen: {e}")
        clicked_ = _objc.selector(clicked_, signature=b"v@:@")
except Exception:
    _NativeMenuTarget = None


# Modul-globale Strong-Reference zum Status-Item. pyobjc-Objekte in
# Hybrid-Event-Loops (Qt + CFRunLoop) werden manchmal zu früh released,
# selbst mit instance-attribute-ref — diese global hält sie bombenfest.
_GLOBAL_TRAY_REF = None


class NativeStatusBar(QObject):
    """QSystemTrayIcon-Ersatz via Subprocess.

    Qt-hostetes NSStatusItem rendert auf macOS 26 das Menubar-Icon nicht
    (Isoliert-Test funktioniert, Qt-Variante nicht — Qts NSApplication
    blockt den Registrierungspfad). Wir spawnen daher einen reinen
    pyobjc-Kind-Prozess der NUR das Tray hostet. Kommunikation per
    JSON-Lines über stdin/stdout.

    Menu-Klicks kommen über stdout zurück und werden via Qt-Signal im
    Main-Thread auf die jeweilige QAction.trigger() gerouted."""

    click_signal = Signal(str)  # action_id string

    def __init__(self, tray_script_path, python_exec):
        super().__init__()
        import subprocess
        self._qmenu = None
        self._id_to_action = {}
        self._proc = subprocess.Popen(
            [python_exec, "-u", tray_script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        log.info(f"NativeStatusBar: Tray-Subprocess gestartet (PID {self._proc.pid})")

        # Stdout-Reader-Thread: wandelt JSON-Lines in Qt-Signals um
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

        # Menu-Klick-Signal -> QAction-Dispatch (im Main-Thread)
        self.click_signal.connect(self._on_click)

    def _read_stdout(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if "click" in msg:
                    self.click_signal.emit(str(msg["click"]))
                elif "ready" in msg:
                    log.info("NativeStatusBar: Tray meldet sich bereit")
        except Exception as e:
            log.warning(f"NativeStatusBar: stdout-reader beendet: {e}")

    def _on_click(self, action_id):
        action = self._id_to_action.get(action_id)
        if action is None:
            log.warning(f"Tray-Click unbekannte Action: {action_id}")
            return
        try:
            action.trigger()
        except Exception as e:
            log.exception(f"QAction.trigger fehlgeschlagen: {e}")

    def _send(self, obj):
        try:
            self._proc.stdin.write(json.dumps(obj) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            log.warning(f"NativeStatusBar: send fehlgeschlagen: {e}")

    def update_state(self, state):
        self._send({"cmd": "title",
                    "value": _NATIVE_ICON_TITLES.get(state, _NATIVE_ICON_TITLES["ready"])})

    def setToolTip(self, text):
        self._send({"cmd": "tooltip", "value": str(text)})

    def build_menu_from_qmenu(self, qmenu):
        self._qmenu = qmenu
        self._id_to_action = {}
        items = self._walk_qmenu(qmenu)
        self._send({"cmd": "menu", "items": items})

    def _walk_qmenu(self, qmenu):
        out = []
        for action in qmenu.actions():
            if action.isSeparator():
                out.append({"sep": True})
                continue
            submenu = action.menu()
            if submenu is not None:
                out.append({
                    "title": action.text().replace("&", ""),
                    "submenu": self._walk_qmenu(submenu),
                })
                continue
            aid = f"a{id(action)}"
            self._id_to_action[aid] = action
            entry = {
                "title": action.text().replace("&", ""),
                "id": aid,
                "enabled": action.isEnabled(),
            }
            if action.isCheckable():
                entry["checked"] = action.isChecked()
            out.append(entry)
        return out

    def hide(self):
        self._send({"cmd": "quit"})
        try:
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def showMessage(self, title, message, *args, **kwargs):
        log.info(f"[Notify] {title}: {message}")


def _make_icon_pixmap(state):
    """Rendert das Mikrofon-Emoji (🎤) via native macOS NSImage+NSString.
    Qts QPainter+QFont rendert "Apple Color Emoji" auf Python-Standalone
    nur lückenhaft (~3% der Pixel); die native AppKit-Text-Pipeline
    funktioniert dagegen zuverlässig — das ist der gleiche Weg, den
    v1 (rumps) via NSStatusItem.title genutzt hat.
    Bei rec/busy overlayen wir einen farbigen Status-Punkt, damit man
    die Aufnahme sieht."""
    emoji = _ICON_EMOJIS.get(state, "🎤")
    try:
        from AppKit import (
            NSImage, NSFont, NSColor, NSBezierPath,
            NSForegroundColorAttributeName, NSFontAttributeName,
        )
        from Foundation import NSMakeSize, NSMakePoint, NSMakeRect, NSString

        # 22pt logisch — macOS-Menubar-Standardhöhe. NSImage bekommt
        # automatisch HiDPI-Representation vom Framework.
        logical = 22.0
        img = NSImage.alloc().initWithSize_(NSMakeSize(logical, logical))
        img.lockFocus()

        font = NSFont.fontWithName_size_("Apple Color Emoji", 16)
        attrs = {NSFontAttributeName: font}
        ns_emoji = NSString.stringWithString_(emoji)
        ns_emoji.drawAtPoint_withAttributes_(NSMakePoint(3.0, 1.0), attrs)

        # Aufnahme-/Busy-Status: kleiner Punkt oben rechts
        if state == "rec":
            NSColor.colorWithRed_green_blue_alpha_(0.86, 0.20, 0.20, 1.0).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(logical - 8, logical - 8, 7, 7)
            ).fill()
        elif state == "busy":
            NSColor.colorWithRed_green_blue_alpha_(0.95, 0.65, 0.15, 1.0).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(logical - 8, logical - 8, 7, 7)
            ).fill()

        img.unlockFocus()

        tiff = img.TIFFRepresentation()
        data = bytes(tiff)
        pm = QPixmap()
        pm.loadFromData(data)
        return pm
    except Exception as e:
        log.warning(f"NSImage-Icon-Rendering fehlgeschlagen: {e}")
        # Fallback: schlichtes graues Quadrat, damit überhaupt WAS da ist
        size = 44
        pm = QPixmap(size, size)
        pm.setDevicePixelRatio(2.0)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(40, 40, 40, 240))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(8, 8, size - 16, size - 16))
        p.end()
        return pm



class HotkeyRecorderDialog(QDialog):
    """Modaler Dialog zum Aufnehmen einer Tastenkombination.

    Der User druckt einfach die gewünschte Kombi (z.B. ⌃⇧), lässt
    los, der Dialog zeigt das Ergebnis und speichert es beim Klick auf
    "Speichern"."""

    _MOD_ORDER = ("ctrl", "alt", "shift", "cmd")

    # Heuristik: Kombis die wir als System-/App-kritisch flaggen. Kein
    # hartes Verbot, nur Confirm vor dem Speichern.
    _CONFLICT_COMBOS = {
        "cmd+c":     "Kopieren",
        "cmd+v":     "Einfügen (wird intern für Auto-Paste benutzt!)",
        "cmd+x":     "Ausschneiden",
        "cmd+z":     "Rückgängig",
        "cmd+y":     "Wiederholen",
        "cmd+s":     "Speichern",
        "cmd+a":     "Alles auswählen",
        "cmd+f":     "Suchen",
        "cmd+q":     "App beenden",
        "cmd+w":     "Fenster schließen",
        "cmd+m":     "Fenster minimieren",
        "cmd+h":     "App ausblenden",
        "cmd+space": "Spotlight",
        "cmd+tab":   "App-Switcher",
        "ctrl+up":   "Mission Control",
    }

    def __init__(self, parent=None, initial="", iqspeakr_app=None):
        super().__init__(parent)
        self.setWindowTitle("Tastenkombination ändern")
        self.setModal(True)
        self.setFocusPolicy(Qt.StrongFocus)
        # WICHTIG für Mac: damit keyPressEvent überhaupt feuert MUSS der
        # Dialog Tastatur-Fokus halten. Bei macOS holt sich modale QDialog
        # den nicht zuverlässig, wenn alle Buttons NoFocus sind. Wir holen
        # ihn explicit beim Zeigen.
        self.resize(520, 280)
        if os.path.exists(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))

        self._mods = set()
        self._key = None
        self._capture_complete = False
        self._captured = (initial or "").strip().lower()
        # Wenn der Dialog von SettingsView geöffnet wird, schickt sie eine
        # Referenz auf IQspeakrApp mit. Damit können wir den globalen
        # Hotkey-Listener supressen während wir tippen — sonst löst jedes
        # Press eines Modifiers eine Aufnahme aus statt nur den Recorder
        # zu füttern.
        self._iq_app = iqspeakr_app
        self._suppress_was = False
        # Timer-Fallback: 700 ms nach dem letzten keyPress finalisieren wir
        # den aktuellen Stand. Auf macOS kommen Modifier-Release-Events
        # nicht immer bei Qt an (pynput-CGEventTap im selben Prozess sieht
        # sie zuerst), deshalb ist Release-only nicht zuverlässig für
        # reine Modifier-Kombis (⌃⇧, ⌘⇧). Der Timer ist das Sicherheitsnetz.
        self._capture_timer = QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._capture_timer.setInterval(700)
        self._capture_timer.timeout.connect(lambda: self._finalize_capture("timer"))

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
            "Mindestens ein Modifier (⌃ ctrl, ⇧ shift, ⌥ alt, ⌘ cmd) "
            "erforderlich. Optional dazu ein Buchstabe, Leertaste, Eingabe, Tab oder F1-F12."
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
            self._error_lbl.setText(
                "Mindestens ein Modifier (⌃ ctrl, ⇧ shift, ⌥ alt, ⌘ cmd) erforderlich."
            )
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

    def showEvent(self, event):
        super().showEvent(event)
        # 1) Fokus selber holen — sonst gehen Key-Events ins Leere wenn
        #    alle Buttons NoFocus sind.
        # 2) Globalen Hotkey-Listener supressen, damit das Tippen einer
        #    Modifier-Kombi keine Aufnahme triggert.
        self.activateWindow()
        self.raise_()
        self.setFocus(Qt.OtherFocusReason)
        if self._iq_app is not None:
            self._suppress_was = bool(getattr(self._iq_app, "_suppress_listener", False))
            self._iq_app._suppress_listener = True

    def hideEvent(self, event):
        # WICHTIG: hideEvent feuert sowohl bei accept() (Speichern) als
        # auch bei reject()/Schließen. closeEvent reicht NICHT, weil
        # accept() das Fenster nur via hide() versteckt - dann würde
        # der globale Listener supressed bleiben und der neue Hotkey
        # wäre stumm.
        if self._iq_app is not None:
            self._iq_app._suppress_listener = self._suppress_was
        try:
            self._capture_timer.stop()
        except Exception:
            pass
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        # Neuer Press nach einem fertigen Capture -> reset und neu aufnehmen
        if self._capture_complete:
            self._mods = set()
            self._key = None
            self._capture_complete = False

        qt_key = event.key()
        captured_what = None
        if qt_key in _QT_MOD_TO_NAME:
            self._mods.add(_QT_MOD_TO_NAME[qt_key])
            captured_what = f"mod={_QT_MOD_TO_NAME[qt_key]}"
        elif qt_key in _QT_NAMED_KEYS:
            self._key = _QT_NAMED_KEYS[qt_key]
            captured_what = f"named={self._key}"
        else:
            t = (event.text() or "").lower()
            if len(t) == 1 and t.isalnum():
                self._key = t
                captured_what = f"char={t}"
        log.debug(
            f"HotkeyRecorder.keyPress captured={captured_what} -> "
            f"{self._build_combo_str()!r}"
        )
        self._refresh_display()
        # Timer für Fallback-Finalisierung neu starten (siehe __init__).
        self._capture_timer.start()
        event.accept()

    def keyReleaseEvent(self, event):
        is_repeat = event.isAutoRepeat()
        qt_key = event.key()
        mods = QApplication.keyboardModifiers()
        m = int(mods.value) if hasattr(mods, "value") else int(mods)
        still_mod_down = bool(
            m & int(Qt.ControlModifier.value) | m & int(Qt.ShiftModifier.value)
            | m & int(Qt.AltModifier.value) | m & int(Qt.MetaModifier.value)
        )
        log.debug(
            f"HotkeyRecorder.keyRelease still_mod_down={still_mod_down} "
            f"_mods={sorted(self._mods)} _key={self._key!r}"
        )
        if is_repeat:
            return
        if (qt_key not in _QT_MOD_TO_NAME) or not still_mod_down:
            self._finalize_capture("keyRelease")
        event.accept()

    def _finalize_capture(self, source):
        """Schreibt den aktuell gehaltenen Stand nach _captured. Wird
        sowohl von keyReleaseEvent als auch vom Timer-Fallback (siehe
        keyPressEvent) aufgerufen — letzterer ist Sicherheitsnetz für
        macOS-Edge-Cases wo Modifier-Release-Events nicht zuverlässig
        bei Qt ankommen (pynput-CGEventTap im selben Prozess kann sich
        einmischen)."""
        combo = self._build_combo_str()
        log.info(
            f"HotkeyRecorder._finalize_capture source={source} "
            f"_mods={sorted(self._mods)} _key={self._key!r} -> combo={combo!r}"
        )
        if combo and self._has_modifier(combo):
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

        # Legende rechts unten: "Weniger [...] Mehr"
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

        # Heatmap als eigene Card. Wenn das Fenster schmaler wird als die
        # Heatmap-Mindestbreite (~620 px inkl. Wochentag-Labels), kommt
        # eine horizontale Scrollbar dazu - das ist besser als die Zellen
        # zu zerquetschen.
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
        self._heatmap.setMinimumWidth(620)
        heat_scroll = QScrollArea()
        heat_scroll.setWidget(self._heatmap)
        heat_scroll.setWidgetResizable(True)
        heat_scroll.setFrameShape(QFrame.NoFrame)
        heat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        heat_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # QScrollArea hat einen winzigen Default-sizeHint (~70 px). Im
        # äußeren VBox-Layout würde die Card sonst auf diese Höhe
        # zerquetscht und die Heatmap durch widgetResizable+ScrollBarOff
        # vertikal abgeschnitten. minimumHeight (kein fixed) so setzen,
        # dass alle 7 Reihen + Legende reinpassen - Card darf weiterhin
        # mit Inhalt wachsen.
        heat_scroll.setMinimumHeight(self._heatmap.minimumHeight())
        heat_scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        hc.addWidget(heat_scroll)
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
            f"Längster Streak: {longest} {'Tag' if longest == 1 else 'Tage'}"
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

        lock_icon = QLabel("\U0001f512")
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

        # Pillen für Varianten — flowen mit FlexLayout-Surrogat
        # (HBoxLayout kann nicht umbrechen; wir nutzen eine simple
        # Wrap-Implementierung über QGridLayout-ähnliche QHBoxLayouts).
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

        # List-Page: vertikal gestapelte Cards in eigener ScrollArea.
        # (Die äußere ScrollArea fängt die ganze Seite, die innere wäre
        # redundant - wir nutzen ein simples QVBoxLayout mit Stretch.)
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

        # Inhalts-Container mit Max-Width 900 px, horizontal zentriert.
        # Bei breitem Fenster bleibt die Card lesbar (kein 1500-px-Form-
        # Row), bei schmalem Fenster (<= 900) skaliert sie mit.
        body = QWidget()
        scroll.setWidget(body)
        outer_h = QHBoxLayout(body)
        outer_h.setContentsMargins(36, 32, 36, 28)
        outer_h.addStretch(1)
        content = QWidget()
        content.setMaximumWidth(900)
        content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        outer_h.addWidget(content, 8)
        outer_h.addStretch(1)
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
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
        # Bei schmalem Fenster (<600 px Card-Breite) Label über den Wert
        # stapeln statt nebeneinander - sonst überquetscht das Layout.
        gl.setRowWrapPolicy(QFormLayout.WrapLongRows)

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
        model_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
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
        # Sekundärer Toggle-Button: schaltet die Ollama-Integration komplett
        # aus oder wieder ein. Kleiner als der Hauptbutton, sichtbar in den
        # passenden States (READY/NEEDS_MODEL/ERROR/DISABLED). Beim Klick im
        # DISABLED-State wird er der primäre "Aktivieren"-Trigger.
        self._toggle_btn = QPushButton()
        self._toggle_btn.setMinimumHeight(34)
        self._toggle_btn.setMinimumWidth(180)
        self._toggle_btn.clicked.connect(self._on_toggle_clicked)
        action_row.addWidget(self._toggle_btn)
        action_row.addStretch(1)
        ob.addLayout(action_row)

        self._cleanup_cb = QCheckBox("Aufnahmen automatisch bereinigen")
        self._cleanup_cb.setChecked(bool(self.app.config.get("cleanup_enabled", True)))
        self._cleanup_cb.toggled.connect(self._on_cleanup_toggled)
        ob.addWidget(self._cleanup_cb)

        # ---- Hinweis-Block: "Wann brauchst du KI-Textbereinigung?" ----
        # Dezent indigofarbener Container mit Erklärung warum Whisper allein
        # in den meisten Fällen reicht. User-Spec 2026-04-27.
        hint_box = QFrame()
        hint_box.setObjectName("OllamaHintBox")
        hint_box.setStyleSheet(
            f"#OllamaHintBox {{"
            f" background: rgba(99, 102, 241, 0.10);"
            f" border: 1px solid rgba(99, 102, 241, 0.35);"
            f" border-radius: 12px;"
            f"}}"
        )
        hb = QVBoxLayout(hint_box)
        hb.setContentsMargins(22, 20, 22, 22)
        hb.setSpacing(10)

        head_row = QHBoxLayout()
        head_row.setSpacing(10)
        info_icon = QLabel()
        info_icon.setPixmap(
            _lucide_icon(_LUCIDE_INFO, 20, THEME_ACCENT).pixmap(20, 20)
        )
        info_icon.setFixedSize(20, 20)
        info_icon.setAlignment(Qt.AlignTop)
        head_row.addWidget(info_icon, 0, Qt.AlignTop)
        head_lbl = QLabel("Wann brauchst du KI-Textbereinigung?")
        f = head_lbl.font(); f.setBold(True); f.setPointSizeF(f.pointSizeF() + 1); head_lbl.setFont(f)
        head_lbl.setStyleSheet(f"color: {THEME_TEXT};")
        head_row.addWidget(head_lbl, 1)
        hb.addLayout(head_row)

        para1 = QLabel(
            "Whisper setzt bereits automatisch <b>Satzzeichen, Großschreibung</b> "
            "und filtert die meisten <b>Füllwörter</b> (ähm, äh) sowie Stotterer "
            "raus. Für klares, ruhiges Diktat reicht das vollkommen."
        )
        para1.setWordWrap(True)
        para1.setTextFormat(Qt.RichText)
        para1.setStyleSheet(f"color: {THEME_TEXT_SECONDARY}; font-size: 13px;")
        hb.addWidget(para1)

        sub_lbl = QLabel("Cleanup nur einschalten, wenn du:")
        sf = sub_lbl.font(); sf.setBold(True); sub_lbl.setFont(sf)
        sub_lbl.setStyleSheet(f"color: {THEME_TEXT}; font-size: 13px;")
        hb.addWidget(sub_lbl)

        bullets = QLabel(
            "•  sehr unkonzentriert sprichst (viele 'ähm', 'halt', 'also', Wortdoppelungen)<br>"
            "•  echtes <b>förmliches Schriftdeutsch</b> möchtest (Style 'Förmlich')<br>"
            "•  eigene Cleanup-Regeln einsetzen willst (Style 'Individuell')"
        )
        bullets.setTextFormat(Qt.RichText)
        bullets.setWordWrap(True)
        bullets.setStyleSheet(
            f"color: {THEME_TEXT_SECONDARY}; font-size: 13px; padding-left: 4px;"
        )
        hb.addWidget(bullets)

        para2 = QLabel(
            "<b>Trade-off:</b> Cleanup kostet je nach Modell und Textlänge "
            "<b>etwa 1-7 Sekunden</b> pro Aufnahme. Ohne Cleanup landet der "
            "Text fast sofort im Zielfeld."
        )
        para2.setWordWrap(True)
        para2.setTextFormat(Qt.RichText)
        para2.setStyleSheet(f"color: {THEME_TEXT_SECONDARY}; font-size: 13px;")
        hb.addWidget(para2)

        ob.addWidget(hint_box)

        v.addWidget(self._ollama_box)

        v.addStretch(1)

        # Signals vom OllamaManager. install_progress (v5) versorgt die
        # Progress-Bar während Tarball-Download + Extract.
        self.app.ollama_mgr.state_changed.connect(self._on_state_changed)
        self.app.ollama_mgr.pull_progress.connect(self._on_pull_progress)
        self.app.ollama_mgr.install_progress.connect(self._on_install_progress)
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
        # iqspeakr_app=self.app gibt dem Dialog Zugriff auf den globalen
        # Listener — er supressed ihn während des Tippens, sonst löst
        # jeder Modifier-Druck im Dialog eine Aufnahme aus.
        dlg = HotkeyRecorderDialog(
            self,
            self.app.config.get("hotkey", ""),
            iqspeakr_app=self.app,
        )
        if dlg.exec() == QDialog.Accepted:
            combo = dlg.result_combo()
            log.info(f"SettingsView._change_hotkey: dialog returned combo={combo!r}")
            if combo:
                # SoT: alles läuft über app._apply_hotkey. Das schreibt
                # config, persistiert, restartet Listener, refresht das
                # Tray-Menu UND emittet hotkey_changed -> unser
                # _on_hotkey_changed-Slot updatet das Label.
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
        if new_model:
            self.app._apply_ollama_model(new_model)

    # --- Slots für Live-Sync von Tray -> SettingsView ---
    def _select_combo_data(self, combo, value):
        """Setzt den Combo auf den Index, dessen userData == value ist.
        Block signals währenddessen, sonst feuern wir einen unnötigen
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

    # --- Ollama-State-Reaktion ---
    def _on_state_changed(self, _state):
        self._refresh_ollama_ui()

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

    def _on_install_progress(self, percent, status):
        """v5: Progress während stillem Ollama-Install. percent=-1 ->
        unbestimmte Bar (Verbinden / Entpacken / Service-Start)."""
        if percent < 0:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(percent)
        self._progress.setVisible(True)
        msg = "Installiere Ollama"
        if status:
            msg = f"Installiere Ollama – {status}"
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
            # v5: stiller Download + Spawn von ollama serve, kein Browser.
            self.app.ollama_mgr.install(model)
        elif state == OLLAMA_INSTALLING or state == OLLAMA_WAITING_FOR_USER:
            # Worker abbrechen - State geht zurück nach NOT_INSTALLED.
            self.app.ollama_mgr.cancel_install()
        elif state == OLLAMA_NEEDS_MODEL:
            # User hat den Pull explizit getriggert - ohne diesen Klick
            # passiert nichts (kein 2-4 GB Auto-Download).
            self.app.ollama_mgr.start_pull(model)
        elif state == OLLAMA_PULLING:
            # Laufenden Pull abbrechen - State geht zurück nach NEEDS_MODEL.
            self.app.ollama_mgr.cancel_pull()
        # READY und DISABLED haben hier keine Action - die werden über den
        # Toggle-Button gehandhabt (siehe _on_toggle_clicked).

    def _on_toggle_clicked(self):
        """Sekundärer Button: schaltet die Ollama-Integration aus / wieder
        ein. Aus jedem State erreichbar (außer WAITING/PULLING wo der
        Hauptbutton der Cancel ist - dort ist der Toggle hidden)."""
        state = self.app.ollama_mgr.state()
        model = self._model_combo.currentData() or self.app.config.get("ollama_model", "llama3.2")
        if state == OLLAMA_DISABLED:
            self.app.config["ollama_disabled"] = False
            save_config(self.app.config)
            self.app.ollama_mgr.enable_integration(model)
        else:
            self.app.config["ollama_disabled"] = True
            save_config(self.app.config)
            self.app.ollama_mgr.disable_integration()

    def _set_action_role(self, role):
        # Property muss neu gepolisht werden, sonst greifen die globalen
        # Property-Selektoren ([role="primary"] etc.) nicht.
        self._action_btn.setProperty("role", role)
        self._action_btn.style().unpolish(self._action_btn)
        self._action_btn.style().polish(self._action_btn)

    def _set_toggle_role(self, role):
        self._toggle_btn.setProperty("role", role)
        self._toggle_btn.style().unpolish(self._toggle_btn)
        self._toggle_btn.style().polish(self._toggle_btn)

    def _refresh_ollama_ui(self):
        state = self.app.ollama_mgr.state()
        # Sichtbarkeit Default zurücksetzen
        self._progress.setVisible(False)
        self._progress_text.setVisible(False)
        # Toggle-Button: Default sichtbar als "Deaktivieren". In WAITING/
        # PULLING blenden wir ihn aus, weil dort der Action-Button schon
        # der Cancel/Abbrechen ist und zwei "Aus"-Buttons den User
        # verwirren.
        self._toggle_btn.setVisible(True)
        self._toggle_btn.setEnabled(True)
        self._toggle_btn.setText("Ollama-Integration deaktivieren")
        self._set_toggle_role("danger")

        if state == OLLAMA_DISABLED:
            self._ollama_status.setText(
                "Ollama ist pausiert. Backend läuft nicht, Cleanup "
                "überspringen."
            )
            self._ollama_status.setStyleSheet(f"color: {THEME_TEXT_MUTED};")
            self._action_btn.setText("Pausiert")
            self._set_action_role("primary")
            self._action_btn.setEnabled(False)
            self._model_combo.setEnabled(False)
            self._toggle_btn.setText("Ollama-Integration aktivieren")
            self._set_toggle_role("primary")
        elif state == OLLAMA_NOT_INSTALLED:
            self._ollama_status.setText(
                "Ollama ist nicht installiert. Klick installiert es still im "
                "Hintergrund (~125 MB Download, kein Tray, kein Dock-Icon)."
            )
            self._ollama_status.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
            self._action_btn.setText("Ollama installieren")
            self._set_action_role("primary")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(True)
        elif state == OLLAMA_INSTALLING:
            self._ollama_status.setText(
                "Ollama wird installiert. Das Binary wird heruntergeladen, "
                "entpackt und der Service gestartet."
            )
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Abbrechen")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(False)
            # Progress-Bar wird durch _on_install_progress gefüllt; hier
            # nur sichtbar machen + initialer Hinweistext setzen, falls
            # das Signal noch nicht gefeuert hat.
            self._progress.setRange(0, 0)
            self._progress.setVisible(True)
            self._progress_text.setText("Installiere Ollama – Verbinde…")
            self._progress_text.setVisible(True)
            self._toggle_btn.setVisible(False)
        elif state == OLLAMA_WAITING_FOR_USER:
            # Legacy-Zustand aus v4 — in v5 nicht mehr erreichbar.
            self._ollama_status.setText("Warte auf Ollama-Service…")
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Abbrechen")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(False)
            self._progress.setRange(0, 0)
            self._progress.setVisible(True)
            self._progress_text.setText("Pinge alle 5s.")
            self._progress_text.setVisible(True)
            self._toggle_btn.setVisible(False)
        elif state == OLLAMA_NEEDS_MODEL:
            mdl = self._model_combo.currentData() or self.app.config.get("ollama_model", "llama3.2")
            self._ollama_status.setText(
                f"Ollama läuft, aber Modell '{mdl}' fehlt noch. "
                "Klick auf den Button startet den Download (je nach "
                "Modell 2-5 GB)."
            )
            self._ollama_status.setStyleSheet(f"color: {THEME_TEXT_SECONDARY};")
            self._action_btn.setText("Modell jetzt herunterladen")
            self._set_action_role("primary")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(True)
        elif state == OLLAMA_PULLING:
            self._ollama_status.setText("Lade Modell herunter...")
            self._ollama_status.setStyleSheet(f"color: {THEME_WARNING};")
            self._action_btn.setText("Abbrechen")
            self._set_action_role("danger")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(False)
            self._progress.setVisible(True)
            self._progress_text.setVisible(True)
            self._toggle_btn.setVisible(False)
        elif state == OLLAMA_READY:
            self._ollama_status.setText("Ollama aktiv. KI-Textbereinigung verfügbar.")
            self._ollama_status.setStyleSheet(f"color: {THEME_SUCCESS}; font-weight: 500;")
            # Bei READY ist nichts zu tun - daher Action-Button greyen
            # statt ein wenig hilfreiches "Browser öffnen" anzubieten.
            self._action_btn.setText("Ollama bereit ✓")
            self._set_action_role("primary")
            self._action_btn.setEnabled(False)
            self._model_combo.setEnabled(True)
        else:  # OLLAMA_ERROR
            self._ollama_status.setText("Fehler. Versuche es erneut.")
            self._ollama_status.setStyleSheet(f"color: {THEME_DANGER};")
            self._action_btn.setText("Erneut versuchen")
            self._set_action_role("primary")
            self._action_btn.setEnabled(True)
            self._model_combo.setEnabled(True)
        # Cleanup-Toggle ist immer aktiv - der User entscheidet selbst.
        # Im DISABLED-State spielt's keine Rolle (Cleanup läuft eh nicht),
        # aber der State soll persistent bleiben.
        self._cleanup_cb.setEnabled(True)


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
        self.setWindowTitle("IQspeakr")
        # Mindestgröße: unter 900x700 wird das Layout nicht mehr sinnvoll
        # darstellbar (Style-Cards in 1 Spalte, Settings-Form-Wrap aktiv).
        self.setMinimumSize(900, 700)
        # Initial-Geometrie aus config; Default 1280x900 beim ersten Start.
        # Die config-Werte werden in resizeEvent debounced gespeichert
        # (siehe _on_resize_save_timer).
        w = int(self.app.config.get("window_width", 1280))
        h_ = int(self.app.config.get("window_height", 900))
        self.resize(max(900, w), max(700, h_))
        # Debouncer für den Resize-Save: feuert 200 ms nach dem letzten
        # Resize-Event und schreibt die finale Größe in config.json.
        self._resize_save_timer = QTimer(self)
        self._resize_save_timer.setSingleShot(True)
        self._resize_save_timer.setInterval(200)
        self._resize_save_timer.timeout.connect(self._persist_window_size)
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

        # Logo: Indigo-Quadrat + weißes Mikro - konsistent zum Theme-Akzent.
        # Die .icns-Datei nutzen wir weiter für Window-/Dock-Icon, hier
        # in der Sidebar wollen wir das markante Brand-Element.
        icon_lbl = QLabel()
        icon_lbl.setPixmap(_make_app_logo_pixmap(28))
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

    def resizeEvent(self, event):
        # Bei jedem Resize den Save-Timer neu starten - nach 200 ms ohne
        # weiteres Resize wird die aktuelle Größe in config persistiert.
        # So vermeiden wir Disk-IO bei jedem Pixel während des Draggens.
        super().resizeEvent(event)
        self._resize_save_timer.start()

    def _persist_window_size(self):
        try:
            self.app.config["window_width"] = int(self.width())
            self.app.config["window_height"] = int(self.height())
            save_config(self.app.config)
        except Exception as e:
            log.warning(f"MainWindow: Fenstergroesse-Persistenz fehlgeschlagen: {e}")

    def closeEvent(self, event):
        # Schließen versteckt nur, App lebt im Tray weiter.
        # Vorher noch die finale Größe persistieren (falls der Timer
        # noch läuft und nicht gefeuert hat).
        if self._resize_save_timer.isActive():
            self._resize_save_timer.stop()
            self._persist_window_size()
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
    notify_sig = Signal(str, str)
    status_sig = Signal(str)
    # Overlay-Show/Hide MUSS über Signal laufen, nicht direkt. Qt-Widgets
    # dürfen nur vom Main-Thread erstellt/sichtbar gemacht werden — aus
    # dem pynput-Listener-Callback (CGEventTap-Thread) direkt aufgerufen
    # crasht das mit SIGABRT in NSWindow-Init.
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
        log.info("=" * 60)
        log.info(f"IQspeakr startet (PID {os.getpid()}, frozen={getattr(sys, 'frozen', False)})")
        self.qapp = qapp
        self._splash = splash
        self.config = load_config()
        log.info(
            f"Config: hotkey={self.config.get('hotkey')!r}, "
            f"whisper={self.config.get('whisper_model')!r}, "
            f"lang={self.config.get('language')!r}, "
            f"overlay={self.config.get('overlay_enabled')}"
        )
        self.recording = False
        self.audio_frames = []
        # Persistenter Audio-Stream: einmal geöffnet, lebt bis zum Quit.
        # Vermeidet PortAudio-Races bei rapidem open/close. CoreAudio-
        # Stop/Close läuft beim Quit im Hintergrund-Thread (Mac-Deadlock-
        # Regel: sd.InputStream.stop()/close() darf nicht im Main-Thread
        # während eines Callbacks laufen).
        self._persistent_stream = None
        # Sperrt Listener kurzzeitig während wir Cmd+V simulieren - sonst
        # sieht pynput die simulierten Keys als Hotkey-Press (Self-Trigger).
        self._suppress_listener = False

        # Pill-Overlay (QWidget) - im Main-Thread erzeugt, thread-safe via
        # Signals. KEIN show() hier - Overlay zeigt sich erst bei
        # set_recording(True), sonst hängt es permanent transparent am
        # unteren Bildschirmrand (User-Verwirrung).
        self.overlay = PillOverlay(enabled=self.config.get("overlay_enabled", True))

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

        self._status_text = "Modell wird geladen..."

        # History + Stats + Wörterbuch + Ollama-Manager. main_window wird
        # lazy beim ersten Open instanziiert (siehe _show_main_window).
        # HistoryStore, StatsStore und DictionaryStore emittieren Qt-Signals,
        # an die HomeView / DashboardView / DictionaryView sich binden, um
        # sich automatisch zu refreshen.
        self.history = HistoryStore(self)
        self.stats = StatsStore(self)
        self.dictionary = DictionaryStore(self)
        try:
            n = self.stats.import_legacy_history(self.history.items())
            if n:
                log.info(f"Stats-Migration: {n} Legacy-Eintraege uebernommen")
        except Exception as e:
            log.warning(f"Stats-Migration fehlgeschlagen: {e}")
        self.ollama_mgr = OllamaManager(self)
        self.ollama_mgr.state_changed.connect(self._on_ollama_state_changed)
        self.main_window = None

        # Tray-Icon über natives NSStatusItem (wie v1/rumps).
        # WICHTIG: Creation muss NACH dem Start der Qt-Event-Loop laufen,
        # sonst überschreibt Qts NSApplication-Init den Status-Item-
        # Registrierungspfad auf macOS 26 (Icon wird nie gerendert).
        self.tray = None  # placeholder; wird in _init_native_tray befüllt
        self._menu = QMenu()
        self._build_menu()
        # singleShot(0) = erster Tick nach qapp.exec() startet
        QTimer.singleShot(0, self._init_native_tray)
        log.info("Native-Tray-Init deferred auf Event-Loop-Start")

    def _init_native_tray(self):
        global _GLOBAL_TRAY_REF
        try:
            tray_script = os.path.join(APP_DIR, "tray_proc.py")
            if not os.path.exists(tray_script):
                log.error(f"tray_proc.py nicht gefunden: {tray_script}")
                return
            self.tray = NativeStatusBar(tray_script, sys.executable)
            self.tray.setToolTip("IQspeakr")
            self.tray.build_menu_from_qmenu(self._menu)
            _GLOBAL_TRAY_REF = self.tray
            log.info("NativeStatusBar (Subprocess) erzeugt — 🎤 jetzt in der Menubar")
        except Exception as e:
            log.exception(f"NativeStatusBar-Init fehlgeschlagen: {e}")

        # Signals -> Slots (automatisch queued wenn emitted aus anderem Thread).
        self.icon_state_sig.connect(self._on_icon_state)
        self.rebuild_menu_sig.connect(self._build_menu)
        self.notify_sig.connect(self._on_notify)
        self.status_sig.connect(self._on_status)
        self.overlay_recording_sig.connect(self.overlay.set_recording)

        # Dock-Click -> Hauptfenster öffnen. macOS sendet QEvent.
        # ApplicationActivate wenn die App den Fokus zurückkriegt (Cmd+Tab,
        # Klick aufs Dock-Icon, Klick aufs Tray). Wir öffnen das Fenster
        # NUR wenn aktuell keins sichtbar ist - sonst poppt es bei jedem
        # Cmd+Tab unkontrolliert auf, was nervt.
        self.qapp.installEventFilter(self)

        # Whisper-Modell laden (Thread)
        threading.Thread(target=self._load_model, daemon=True).start()

        # Global Hotkey-Listener (pynput). Benötigt Bedienungshilfen-
        # Berechtigung auf macOS (Systemeinstellungen -> Datenschutz ->
        # Bedienungshilfen). Beim ersten Start kommt ein System-Prompt.
        # Ohne Permission schmeißt pynput intern (im Listener-Thread) —
        # deshalb brauchen wir einen Watchdog, der den Listener-Zustand
        # kurz nach dem Start prüft.
        self._listener = None
        self._start_hotkey_listener()
        # Watchdog: nach 1.5s prüfen, ob der Listener-Thread noch läuft.
        # pynput stirbt bei fehlender Accessibility-Permission ggf. leise
        # (Objective-C Exception im Hintergrund-Thread).
        QTimer.singleShot(1500, self._check_listener_health)

        # Hintergrund-Polling: prüft alle 3s, ob TCC inzwischen granted ist.
        # Damit muss der User nach dem Schalter-Aktivieren NICHT zwingend
        # "Hab ich gemacht" klicken — App merkt's automatisch und startet
        # den Listener neu.
        self._tcc_was_granted = False  # initial state
        self._tcc_poll_timer = QTimer(self)
        self._tcc_poll_timer.setInterval(3000)
        self._tcc_poll_timer.timeout.connect(self._poll_accessibility_status)
        self._tcc_poll_timer.start()

    def _start_hotkey_listener(self):
        try:
            self._listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            # daemon MUSS vor start() gesetzt werden.
            self._listener.daemon = True
            self._listener.start()
            log.info("pynput Keyboard-Listener gestartet")
        except Exception as e:
            # Typisch auf Mac bei fehlender Accessibility-Permission:
            # OSError oder ImportError aus Quartz. Wir loggen, zeigen
            # dem User eine klare Fehlermeldung und öffnen den
            # System-Einstellungen-Dialog.
            log.exception(f"Hotkey-Listener konnte nicht starten: {e}")
            self._set_status("Hotkey deaktiviert (Accessibility?)")
            QTimer.singleShot(500, self._show_accessibility_hint)

    def _check_listener_health(self):
        """Prüft ob der pynput-Listener Events kriegt. pynput nutzt auf macOS
        CGEventTap — das braucht *Eingabeüberwachung* (Input Monitoring),
        NICHT Bedienungshilfen. Wir checken die korrekte TCC-Permission via
        CGPreflightListenEventAccess (CoreGraphics Public API)."""
        try:
            running = bool(self._listener and self._listener.running)
            alive = bool(self._listener and self._listener.is_alive())
        except Exception:
            running, alive = False, False

        # Input Monitoring (Eingabeüberwachung) — das ist was CGEventTap braucht
        input_monitoring_ok = True
        try:
            from Quartz import CGPreflightListenEventAccess
            input_monitoring_ok = bool(CGPreflightListenEventAccess())
        except Exception as e:
            log.warning(f"CGPreflightListenEventAccess nicht abrufbar: {e}")

        if not (running and alive and input_monitoring_ok):
            log.warning(
                f"Listener-Watchdog: running={running} alive={alive} "
                f"input_monitoring={input_monitoring_ok} -> "
                "Eingabeueberwachung-Berechtigung fehlt."
            )
            self._set_status("Hotkey deaktiviert (Eingabeüberwachung?)")
            self._show_accessibility_hint()
        else:
            log.info("Listener-Watchdog: Hotkey-Erkennung laeuft sauber.")

    def _show_accessibility_hint(self, manual_trigger=False):
        """Schritt-für-Schritt-Wizard für Eingabeüberwachung
        (Input Monitoring) — das ist was pynput's CGEventTap auf macOS
        braucht. Öffnet ausschließlich den Eingabeüberwachung-Tab
        (kein Finder mehr), zeigt klare Anleitung."""
        if getattr(self, "_accessibility_hint_shown", False) and not manual_trigger:
            return
        self._accessibility_hint_shown = True

        # WICHTIG: Privacy_ListenEvent = Eingabeüberwachung,
        # NICHT Privacy_Accessibility = Bedienungshilfen. pynput braucht
        # ersteres für globale Hotkeys via CGEventTap.
        try:
            subprocess.Popen([
                "open",
                "x-apple.systempreferences:com.apple.preference.security"
                "?Privacy_ListenEvent",
            ])
        except Exception as e:
            log.warning(f"System-Einstellungen konnten nicht geoeffnet werden: {e}")

        box = QMessageBox()
        box.setWindowTitle("IQspeakr - Eingabeüberwachung aktivieren")
        box.setIcon(QMessageBox.Information)
        box.setText(
            "IQspeakr braucht ZWEI Berechtigungen, beide unter\n"
            "Datenschutz & Sicherheit:"
        )
        box.setInformativeText(
            "1. EINGABEUEBERWACHUNG (für den globalen Hotkey)\n"
            "    So kommst du hin:\n"
            "    Apple-Menü -> Systemeinstellungen -> Datenschutz & Sicherheit\n"
            "    -> Eingabeüberwachung\n"
            "    (Dieser Tab öffnet sich gleich automatisch.)\n"
            "    Dann: '+' Knopf -> Programme -> IQspeakr -> Öffnen\n"
            "    -> Schalter neben IQspeakr EIN.\n\n"
            "2. BEDIENUNGSHILFEN (für Auto-Paste mit Cmd+V)\n"
            "    So kommst du hin:\n"
            "    Apple-Menü -> Systemeinstellungen -> Datenschutz & Sicherheit\n"
            "    -> Bedienungshilfen\n"
            "    (Im selben Settings-Fenster: links in der Liste auf\n"
            "    'Bedienungshilfen' klicken.)\n"
            "    Dann: '+' Knopf -> Programme -> IQspeakr -> Öffnen\n"
            "    -> Schalter neben IQspeakr EIN.\n\n"
            "Mit TouchID/Passwort jeweils bestätigen.\n\n"
            "Dieses Fenster kannst du offen lassen — IQspeakr erkennt\n"
            "automatisch, sobald beide Schalter aktiv sind."
        )
        ok_btn = box.addButton("Hab ich gemacht", QMessageBox.AcceptRole)
        later_btn = box.addButton("Später", QMessageBox.RejectRole)
        box.setDefaultButton(ok_btn)
        box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
        box.exec()

        if box.clickedButton() is ok_btn:
            log.info("User bestaetigt Bedienungshilfen - starte Listener neu")
            try:
                if self._listener:
                    self._listener.stop()
            except Exception:
                pass
            self._start_hotkey_listener()
            QTimer.singleShot(1500, self._check_listener_health_after_grant)
        else:
            # User klickt 'Später' — Wizard nicht erneut zeigen in dieser
            # Session. Re-Trigger geht über Tray-Menü.
            log.info("User verschiebt Bedienungshilfen-Setup")
            self._set_status("Bedienungshilfen fehlen - via Tray-Menü erneut einrichten")

    def _check_listener_health_after_grant(self):
        """Wird nach 'Hab ich gemacht' aufgerufen. Prüft Eingabeüberwachung
        (NICHT Bedienungshilfen). Wenn nicht erteilt: Notification, kein
        erneuter Wizard (kein Endlos-Loop)."""
        try:
            from Quartz import CGPreflightListenEventAccess
            trusted = bool(CGPreflightListenEventAccess())
        except Exception:
            trusted = False

        if trusted:
            log.info("Eingabeueberwachung erteilt - Hotkey laeuft jetzt")
            self._set_status("")
            self._notify(
                "IQspeakr",
                "Eingabeüberwachung aktiv - Hotkey funktioniert jetzt.",
            )
        else:
            log.warning("Trotz 'Hab ich gemacht': TCC sagt noch nicht trusted")
            self._set_status("Eingabeüberwachung fehlt - via Tray-Menü erneut einrichten")
            self._notify(
                "Eingabeüberwachung fehlt",
                "Klicke aufs Tray-Icon und wähle 'Eingabeüberwachung einrichten' "
                "für die Anleitung.",
            )

    def _trigger_accessibility_setup_manually(self):
        """Wird vom Tray-Menü aufgerufen, damit der User den Wizard
        jederzeit erneut starten kann (ohne App-Neustart)."""
        self._accessibility_hint_shown = False
        self._show_accessibility_hint(manual_trigger=True)

    def _poll_accessibility_status(self):
        """Wird alle 3s aufgerufen. Erkennt automatisch wenn eine der beiden
        Permissions erteilt wird:
          - Eingabeüberwachung (CGPreflightListenEventAccess) -> Hotkey
          - Bedienungshilfen (AXIsProcessTrusted)               -> Cmd+V
        Listener wird neu gestartet, User per Notification informiert."""
        try:
            from Quartz import CGPreflightListenEventAccess
            from ApplicationServices import AXIsProcessTrusted
            input_now = bool(CGPreflightListenEventAccess())
            access_now = bool(AXIsProcessTrusted())
        except Exception:
            return

        # State-Tracking — initial sync auf erstem Lauf
        if not hasattr(self, "_access_was_granted"):
            self._access_was_granted = access_now

        # Eingabeüberwachung wurde gerade erteilt -> Listener neu starten
        if input_now and not self._tcc_was_granted:
            log.info("Auto-Polling: Eingabeueberwachung wurde gerade erteilt!")
            self._tcc_was_granted = True
            try:
                if self._listener:
                    self._listener.stop()
            except Exception:
                pass
            self._start_hotkey_listener()
            self._set_status("")
            if access_now:
                self._notify(
                    "IQspeakr",
                    "Beide Berechtigungen aktiv - Hotkey + Auto-Paste funktionieren!",
                )
            else:
                self._notify(
                    "IQspeakr - Hotkey aktiv",
                    "Für Auto-Paste fehlt noch 'Bedienungshilfen'. "
                    "Im selben Settings-Fenster aktivieren.",
                )
        elif input_now:
            self._tcc_was_granted = True

        # Bedienungshilfen wurde gerade erteilt -> Auto-Paste funktioniert
        if access_now and not self._access_was_granted:
            log.info("Auto-Polling: Bedienungshilfen wurde gerade erteilt!")
            self._access_was_granted = True
            if input_now:
                self._notify(
                    "IQspeakr",
                    "Beide Berechtigungen aktiv - Hotkey + Auto-Paste funktionieren!",
                )
            else:
                self._notify(
                    "IQspeakr - Auto-Paste aktiv",
                    "Für Hotkey fehlt noch 'Eingabeüberwachung'. "
                    "Im selben Settings-Fenster aktivieren.",
                )

        # Wechsel von True nach False = Permission entzogen
        if self._tcc_was_granted and not input_now:
            log.warning("Auto-Polling: Eingabeueberwachung wurde entzogen")
            self._tcc_was_granted = False
            self._set_status("Eingabeüberwachung fehlt")
        if self._access_was_granted and not access_now:
            log.warning("Auto-Polling: Bedienungshilfen wurde entzogen")
            self._access_was_granted = False

    # --- Slots (laufen immer im Main-Thread) ---

    def _on_icon_state(self, state):
        if self.tray is None:
            return  # Tray wird deferred initialisiert — Zustand holt sich der
                    # nächste Aufruf nach Init.
        try:
            self.tray.update_state(state)
        except Exception as e:
            log.warning(f"Icon-Update fehlgeschlagen: {e}")

    def _on_notify(self, title, message):
        if self.tray is None:
            log.info(f"[Notify pre-tray] {title}: {message}")
            return
        self.tray.showMessage(title, message)

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

    def _notify(self, title, message):
        self.notify_sig.emit(title, message)

    def _set_status(self, text):
        self.status_sig.emit(text)

    def _refresh_menu(self):
        self.rebuild_menu_sig.emit()

    # --- Hauptfenster + Ollama-State ---

    def _show_main_window(self):
        """Lazy-init beim ersten Aufruf. Subsequent calls bringen das
        bestehende Fenster nach vorn. closeEvent versteckt nur, App lebt
        im Tray weiter."""
        if self.main_window is None:
            self.main_window = MainWindow(self)
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def eventFilter(self, obj, event):
        """Dock-Click bzw. Cmd+Tab-zurück: wenn die App ApplicationActivate
        bekommt und aktuell kein Hauptfenster sichtbar ist, öffnen wir's.
        Sind schon Fenster da, lassen wir sie in Ruhe (User könnte gerade
        einen Dialog offen haben)."""
        if event.type() == QEvent.ApplicationActivate:
            mw = self.main_window
            if mw is None or not mw.isVisible() or mw.isMinimized():
                # Im Worker-Thread sicher über Main-Thread queuen.
                QTimer.singleShot(0, self._show_main_window)
        return False  # nie konsumieren - andere Filter sollen weiter sehen

    def _on_ollama_state_changed(self, state):
        """Halte ollama_available im Sync mit dem Manager-State.
        cleanup_enabled (Master-Switch) wird NICHT automatisch überschrieben
        - der User entscheidet das selbst in den Einstellungen. Wenn
        cleanup aktiv ist aber Ollama nicht läuft, fügt _cleanup_text
        einfach den Whisper-Rohtext ein (no-op bei nicht-ready)."""
        self.ollama_available = (state == OLLAMA_READY)
        self.rebuild_menu_sig.emit()

    def _reload_hotkey(self):
        """Re-applies den aktuellen Config-Wert komplett (parser, label,
        listener-restart). Backwards-Compat-Wrapper - der echte Setter
        ist _apply_hotkey, beide Pfade laufen darüber."""
        self._apply_hotkey(self.config.get("hotkey", "ctrl+shift"), restart_listener=True)

    def _reload_whisper_model(self):
        """Whisper-Modell neu laden (im Hintergrund-Thread). Wird von
        SettingsView.whisper-Combo aufgerufen."""
        self.model = None
        self._set_status(f"Lade Whisper '{self.config['whisper_model']}'...")
        threading.Thread(target=self._load_model, daemon=True).start()

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

        # Eingabeüberwachung-Wizard manuell triggern
        access_act = QAction("Eingabeüberwachung einrichten", self._menu)
        access_act.triggered.connect(lambda _=False: self._trigger_accessibility_setup_manually())
        self._menu.addAction(access_act)

        self._menu.addSeparator()
        quit_act = QAction("Beenden", self._menu)
        quit_act.triggered.connect(lambda _=False: self._quit())
        self._menu.addAction(quit_act)

        # Subprocess-Tray synchron halten (Haken bei Hotkey/Sprache/Modell
        # werden nur gesetzt wenn wir das Menu neu an den Tray schicken).
        if self.tray is not None:
            try:
                self.tray.build_menu_from_qmenu(self._menu)
            except Exception as e:
                log.warning(f"Tray-Menu-Sync fehlgeschlagen: {e}")

    def _build_hotkey_submenu(self, sub):
        # Mac-typische Modifier-Presets. "cmd" ersetzt "win" aus der
        # Windows-Version.
        # WICHTIG zur Reihenfolge: setChecked() MUSS nach addAction() in den
        # QActionGroup laufen. Bei exclusive=True wirft das Group jede
        # Check-Änderung VOR dem Beitritt weg, sobald ein zweites Action
        # die Gruppe betritt - dann steht im Tray das Haken auf der falschen
        # Position. Hier deshalb erst Group, dann setChecked.
        hotkey_options = ["ctrl+shift", "ctrl", "shift", "alt", "cmd"]
        group = QActionGroup(sub)
        group.setExclusive(True)
        current = self.config.get("hotkey", "")
        current_matched = False
        for opt in hotkey_options:
            a = QAction(f"{hotkey_display(opt)} halten", sub)
            a.setCheckable(True)
            a.triggered.connect(self._make_hotkey_callback(opt))
            group.addAction(a)
            sub.addAction(a)
            if current == opt:
                a.setChecked(True)
                current_matched = True
        sub.addSeparator()
        custom = QAction("Eigene Kombination...", sub)
        custom.setCheckable(True)
        custom.triggered.connect(lambda _=False: self._custom_hotkey())
        group.addAction(custom)
        sub.addAction(custom)
        # Wenn der aktuelle Hotkey keine Preset-Option ist, zählt er als "custom".
        if not current_matched and current:
            custom.setText(f"Eigene Kombination: {hotkey_display(current)}...")
            custom.setChecked(True)
        log.debug(
            f"_build_hotkey_submenu: current={current!r} matched={current_matched} "
            f"custom_checked={custom.isChecked()}"
        )
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
          7. Toast-Notification (nur wenn die Änderung user-initiiert
             aussieht — beim _reload_hotkey-Wrapper unterdrücken wir
             sie via notify=False)
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
            self._start_hotkey_listener()
        self._refresh_menu()
        # Subscriber benachrichtigen (SettingsView hört hier mit).
        self.hotkey_changed.emit(hotkey_str)
        # Toast nur bei echter Änderung — beim Re-Apply via _reload_hotkey
        # nach Permission-Grant ist der Wert unverändert, kein Bedarf zu
        # nerven.
        if previous != hotkey_str:
            self._notify("IQspeakr", f"Neuer Hotkey: {self.hotkey_label} halten")

    def _custom_hotkey(self):
        dlg = HotkeyRecorderDialog(
            initial=self.config.get("hotkey", ""),
            iqspeakr_app=self,
        )
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
                f"Das Modell ist noch nicht auf deinem Mac.\n"
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
        triggert den Download bewusst per Button in der Settings-View
        (NEEDS_MODEL-State im OllamaManager)."""
        if model_name == self.config.get("ollama_model"):
            return
        self.config["ollama_model"] = model_name
        save_config(self.config)
        self._refresh_menu()
        self.ollama_model_changed.emit(model_name)
        # Manager neu prüft - liefert READY (Modell schon da) oder
        # NEEDS_MODEL (User klickt dann den Pull-Button).
        if self.ollama_mgr.state() in (OLLAMA_READY, OLLAMA_NEEDS_MODEL):
            self.ollama_mgr.refresh_state(model_name)
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
            subprocess.run(["open", CONFIG_PATH])
        except Exception as e:
            log.warning(f"open_config: {e}")

    def _quit(self):
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass
        # CoreAudio-Deadlock-Regel: stop()/close() auf sd.InputStream darf
        # nicht synchron im Main-Thread laufen. Im Hintergrund-Thread ist
        # es sicher. Qt.quit() wartet NICHT auf den Thread, aber das Prozess-
        # Ende räumt ihn trotzdem sauber ab (daemon).
        stream = self._persistent_stream
        self._persistent_stream = None
        if stream is not None:
            threading.Thread(
                target=self._close_stream_async, args=(stream,), daemon=True,
            ).start()
        if self.tray is not None:
            self.tray.hide()
        self.qapp.quit()

    def _close_stream_async(self, stream):
        try:
            stream.stop()
            stream.close()
        except Exception as e:
            log.warning(f"Stream-Stopp Fehler: {e}")

    # --- Modell laden ---

    def _load_model(self):
        try:
            size = self.config["whisper_model"]
            # Device-Auswahl: Default CPU (stabil). MPS gibt's zwar auf
            # Apple Silicon und ist 3-5x schneller, crasht aber bei
            # openai-whisper in der qkv_attention-Schicht (Fatal Python
            # error: Aborted). Opt-in möglich via config.json
            # "use_mps": true — wer es testen will kann das setzen.
            device = "cpu"
            if self.config.get("use_mps") and _HAS_TORCH:
                try:
                    if torch.backends.mps.is_available():
                        device = "mps"
                except Exception:
                    pass
            log.info(f"Lade Whisper-Modell '{size}' (openai-whisper, device={device})...")
            self.model = whisper.load_model(size, device=device)
            log.info("Whisper-Modell erfolgreich geladen")
        except Exception:
            log.exception("FATAL: Whisper-Laden ist gescheitert")
            self._set_status("Fehler beim Modell-Laden")
            self._notify("IQspeakr - Fehler", "Modell-Laden gescheitert. Siehe IQspeakr.log.")
            return

        # Persistenten Audio-Stream öffnen - bleibt bis zum Quit aktiv.
        # WICHTIG: Nur EINMAL öffnen. Bei Modell-Wechsel wird _load_model
        # erneut gestartet — der bestehende Stream darf dann nicht über-
        # schrieben werden, sonst entstehen zwei konkurrierende Streams
        # die das Mikrofon teilen (Whisper liefert dann leere Strings).
        if self._persistent_stream is not None:
            log.info("Audio-Stream laeuft bereits - ueberspringe Re-Init")
            self._set_status("Bereit")
            return
        try:
            self._persistent_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self._audio_callback,
            )
            self._persistent_stream.start()
            log.info("Persistenter Audio-Stream gestartet")
        except Exception as e:
            log.exception(f"Audio-Stream-Init-Fehler: {e}")
            self._set_status("Mikrofon-Fehler (Permission?)")
            self._notify(
                "IQspeakr - Mikrofon nötig",
                "Mikrofon-Zugriff fehlt oder kein Mikro gefunden. "
                "Systemeinstellungen > Datenschutz > Mikrofon prüfen, "
                "dann App neu starten.",
            )
            try:
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Microphone",
                ])
            except Exception:
                pass
        # Ollama-Status: wenn der User die Integration in den Settings
        # deaktiviert hat (config.ollama_disabled), pingen wir nicht und
        # setzen den State direkt auf DISABLED. Sonst Standard-Detection.
        try:
            if self.config.get("ollama_disabled"):
                log.info("Ollama-Integration ist per Config deaktiviert - kein Polling")
                self.ollama_mgr.disable_integration()
            else:
                self.ollama_mgr.refresh_state(self.config.get("ollama_model"))
        except Exception as e:
            log.warning(f"Ollama refresh_state fehlgeschlagen: {e}")
        self._set_status("Bereit")
        self._notify(
            "IQspeakr - Modell geladen",
            f"Whisper '{self.config['whisper_model']}' ist bereit.\n"
            f"{self.hotkey_label} gedrückt halten = Aufnahme\n"
            f"{self.hotkey_label} 2x tippen = Daueraufnahme\n"
            f"Tray -> Hauptfenster öffnen für History/Stats/Style.",
        )

    def _cleanup_text(self, text):
        # Ollama-Status kommt jetzt aus dem Manager (state_changed-Signal
        # synchronisiert self.ollama_available). Style-Prompt wird aus der
        # config gebaut (formal/locker/sehr_locker/custom).
        if not self.cleanup_enabled or not self.ollama_mgr.is_ready():
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
        except Exception:
            return text

    def toggle_cleanup(self, _sender):
        # Ollama-Status frisch holen, falls Service inzwischen ans Netz ging.
        if not self.ollama_mgr.is_ready():
            self.ollama_mgr.refresh_state(self.config.get("ollama_model"))
            # refresh_state ist asynchron — der State wird kurz danach via
            # state_changed-Signal in self.ollama_available reflektiert.
            # Wir reagieren auf den aktuellen Stand zum Klick-Zeitpunkt.
            self._notify("IQspeakr", "Ollama läuft nicht. Starte Ollama.app zuerst.")
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
        log.info(f"Transkribiere: {len(audio_data)} Samples, Peak: {np.max(np.abs(audio_data)):.4f}")

        try:
            lang = self.config.get("language")
            log.info(f"Starte Whisper-Transkription (Sprache: {lang})...")
            try:
                # openai-whisper nimmt das numpy-Array direkt — kein tempfile
                # nötig. fp16=False, weil MPS in manchen Torch-Versionen
                # fp16-Probleme hat. beam_size=1 und condition_on_previous_text
                # analog zur Windows-Version für Speed.
                result = self.model.transcribe(
                    audio_data,
                    language=lang,
                    fp16=False,
                    beam_size=1,
                    condition_on_previous_text=False,
                )
                raw_text = (result.get("text") or "").strip()
                detected_lang = result.get("language", "?")
                log.info(f"Whisper-Ergebnis: '{raw_text}' (Sprache: {detected_lang})")
            except Exception as e:
                log.error(f"Whisper-Fehler: {e}")
                self._notify("IQspeakr - Fehler", str(e)[:100])
                return

            if raw_text:
                # Wörterbuch IMMER zuerst — auch ohne Ollama. Ollama bekommt
                # die korrigierten Schreibweisen + den Hinweis sie nicht
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
        """Simuliert Cmd+V (Mac-Paste). Sperrt währenddessen den eigenen
        pynput-Listener, damit der die simulierten Keys nicht als Hotkey-
        Press missdeutet (Self-Trigger-Bug)."""
        import time
        # Mini-Delay, damit das OS den Hotkey-Key-Up sauber verarbeitet hat
        # bevor wir Cmd+V simulieren. 50 ms sind spürbar schneller als die
        # früheren 300 ms und reichen in der Praxis.
        time.sleep(0.05)
        self._suppress_listener = True
        try:
            self._kb_controller.press(Key.cmd)
            self._kb_controller.press('v')
            self._kb_controller.release('v')
            self._kb_controller.release(Key.cmd)
            log.info("Cmd+V via pynput ausgefuehrt")
        except Exception as e:
            log.error(f"Paste-Fehler: {e}")
        finally:
            # Kleines Delay damit alle Key-Events durch sind, bevor
            # Listener wieder zuhört.
            threading.Timer(0.15, self._unsuppress_listener).start()
        log.info(f"Eingefuegt: '{text}'")

    def _unsuppress_listener(self):
        self._suppress_listener = False
        # Pressed-Keys-State zurücksetzen, damit ein dort hängender
        # Modifier nicht beim nächsten echten Press-Event hindert.
        self._pressed_keys.clear()


# =====================================================================
#  Main-Entry: QApplication.exec() hält den Main-Thread.
# =====================================================================

def main():
    # multiprocessing.freeze_support ist auf macOS nicht zwingend
    # (spawn-Start statt fork), kostet aber nix — mitnehmen für
    # py2app-Bundles.
    import multiprocessing
    multiprocessing.freeze_support()

    log.info(f"main() start - Python {sys.version.split()[0]}")

    # QApplication + Splash existieren bereits aus dem frühen Bootstrap
    # ganz oben in dieser Datei. Hier nur die Referenz übernehmen.
    qapp = _qapp
    splash = _splash
    # v4: Dock-Icon ist da. LSUIElement ist aus dem Info.plist raus, der
    # NSStatusItem-Tray läuft als Subprocess (von Activation-Policy
    # unabhängig). Dock-Klick -> Hauptfenster: wird in IQspeakrApp via
    # QApplication.installEventFilter (ApplicationActivate) gehandelt.

    # Globales Theme + Schrift-Setup. Wirkt auf alle Widgets, die nach
    # diesem Aufruf erstellt werden — auch das MainWindow (lazy beim
    # ersten Tray-Click).
    apply_app_theme(qapp)
    if os.path.exists(APP_ICON_PATH):
        qapp.setWindowIcon(QIcon(APP_ICON_PATH))

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

    # v5: gespawnten ollama serve beim App-Quit beenden, damit kein
    # waisen-Backend zurückbleibt. aboutToQuit feuert nach dem letzten
    # Window-Close + bevor Qt die Event-Loop beendet.
    qapp.aboutToQuit.connect(app.ollama_mgr.shutdown)

    log.info("Qt Event-Loop uebernimmt (qapp.exec)...")
    # Referenz auf `app` am Leben halten, sonst GC's Qt-Tray-Icon weg.
    qapp._iqspeakr_app = app
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
