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
import urllib.request
import logging
import sys
import time as _time
from pathlib import Path

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

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget,
    QMessageBox, QInputDialog, QMainWindow,
)
from PySide6.QtGui import (
    QIcon, QPixmap, QAction, QActionGroup, QPainter, QColor,
    QGuiApplication,
)

# --- Pfade ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_CONFIG = os.path.join(APP_DIR, "config.json")
USER_DIR = str(Path.home() / "IQspeakr")
CONFIG_PATH = os.path.join(USER_DIR, "config.json")

os.makedirs(USER_DIR, exist_ok=True)
if not os.path.exists(CONFIG_PATH) and os.path.exists(BUNDLE_CONFIG):
    import shutil
    shutil.copy2(BUNDLE_CONFIG, CONFIG_PATH)

OLLAMA_URL = "http://localhost:11434/api/generate"
SAMPLE_RATE = 16000


# =====================================================================
#  Qt-basiertes Pill-Overlay (stabil, thread-safe via Signals).
# =====================================================================

class PillOverlay(QWidget):
    """Always-on-top Pill mit 7 Live-Waveform-Balken unten-mittig.
    Audio-Thread schreibt nur atomische Python-Attribute (thread-safe in
    CPython), der QTimer im Main-Thread liest sie - keine Cross-Thread
    Qt-Signals aus C-Callbacks noetig (vermeidet Stack-Races unter
    Whisper/Torch-Parallelbetrieb)."""

    BAR_COUNT = 7
    W = 180
    H = 36
    IDLE_ALPHA = 0.18
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
        self._target_alpha = self.IDLE_ALPHA
        self._current_alpha = self.IDLE_ALPHA

        if not enabled:
            log.info("PillOverlay: deaktiviert (config.overlay_enabled=false)")
            return

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.resize(self.W, self.H)

        screen = QGuiApplication.primaryScreen().availableGeometry()
        x = screen.x() + (screen.width() - self.W) // 2
        y = screen.y() + screen.height() - self.H - self.MARGIN_BOTTOM
        self.move(x, y)

        self._timer = QTimer(self)
        self._timer.setInterval(40)  # ~25 fps
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self.setWindowOpacity(self.IDLE_ALPHA)

    def _tick(self):
        diff = self._target_alpha - self._current_alpha
        if abs(diff) > 0.005:
            self._current_alpha += diff * 0.2
            self.setWindowOpacity(self._current_alpha)
        # Decay damit Balken bei Stille ruhig abfallen.
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
        if not self.enabled:
            return
        self._target_alpha = self.ACTIVE_ALPHA if on else self.IDLE_ALPHA
        if not on:
            self._levels = [0.0] * self.BAR_COUNT


CLEANUP_PROMPT = """Du bereinigst gesprochene Sprache minimal-invasiv. WICHTIG: Du DARFST den Text NICHT umformulieren oder paraphrasieren. Der Sprecher soll seinen eigenen Stil wiedererkennen.

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
    "ctrl": "Ctrl", "control": "Ctrl",
    "shift": "Shift",
    "alt": "Alt", "option": "Alt",
    "cmd": "Win", "command": "Win", "win": "Win",
    "space": "Space",
    "enter": "Enter",
    "tab": "Tab",
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


def _whisper_model_cached(size):
    # faster-whisper laedt Modelle aus Huggingface-Cache (andere Struktur
    # als openai-whisper). Konservativ: True zurueckgeben, dann ueberspringt
    # die UI den "herunterladen?"-Dialog. Download passiert transparent beim
    # ersten load.
    return True


# --- Config ---
DEFAULT_CONFIG = {
    "hotkey": "ctrl",
    "whisper_model": "small",
    "ollama_model": "llama3.2",
    "cleanup_enabled": True,
    "language": "de",
    "overlay_enabled": True,
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
#  Haupt-App: QObject mit Signals fuer thread-safe GUI-Updates.
# =====================================================================

class IQspeakrApp(QObject):

    # Signals, die Worker-Threads emittieren koennen, um die GUI
    # (tray-icon, menue, notifications) im Main-Thread zu aktualisieren.
    icon_state_sig = Signal(str)
    rebuild_menu_sig = Signal()
    notify_sig = Signal(str, str)
    status_sig = Signal(str)

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
        self.overlay = PillOverlay(enabled=self.config.get("overlay_enabled", True))
        if self.overlay.enabled:
            self.overlay.show()

        self._stream_lock = threading.Lock()
        self.model = None
        self.hotkey_matchers = parse_hotkey(self.config["hotkey"])
        self.hotkey_label = hotkey_display(self.config["hotkey"])
        self._single_modifier_mode = _hotkey_is_single_modifier(self.config["hotkey"])

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

        # Tray-Icon
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(_make_icon_pixmap("ready")))
        self.tray.setToolTip("IQspeakr")
        self._menu = QMenu()
        self._build_menu()
        self.tray.setContextMenu(self._menu)
        self.tray.show()

        # Signals -> Slots (automatisch queued wenn emitted aus anderem Thread).
        self.icon_state_sig.connect(self._on_icon_state)
        self.rebuild_menu_sig.connect(self._build_menu)
        self.notify_sig.connect(self._on_notify)
        self.status_sig.connect(self._on_status)

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

    # --- Menue-Bau ---

    def _build_menu(self):
        """QMenu neu aufbauen. Wird bei jedem Config-Change aufgerufen."""
        self._menu.clear()

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
        hotkey_options = ["ctrl", "cmd", "alt", "shift"]
        group = QActionGroup(sub)
        group.setExclusive(True)
        for opt in hotkey_options:
            a = QAction(f"{hotkey_display(opt)} halten  ({opt})", sub)
            a.setCheckable(True)
            a.setChecked(self.config["hotkey"] == opt)
            a.triggered.connect(self._make_hotkey_callback(opt))
            group.addAction(a)
            sub.addAction(a)
        sub.addSeparator()
        custom = QAction("Eigene Kombination...", sub)
        custom.triggered.connect(lambda _=False: self._custom_hotkey())
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
        self._single_modifier_mode = _hotkey_is_single_modifier(hotkey_str)
        self._pressed_keys.clear()
        self._combo_active = False
        self._hold_mode = False
        self._continuous_mode = False
        save_config(self.config)
        self._refresh_menu()
        self._notify("IQspeakr", f"Neuer Hotkey: {self.hotkey_label} halten")

    def _custom_hotkey(self):
        text, ok = QInputDialog.getText(
            None, "Eigene Tastenkombination",
            "Gib deine Kombination ein, z.B.:\n\n"
            "  ctrl+space\n"
            "  alt+shift+r\n"
            "  ctrl+shift+space\n"
            "  f8\n\n"
            "Verfuegbare Tasten: ctrl, alt, shift, win,\n"
            "space, enter, tab, f1-f12, oder ein Buchstabe (a-z)",
            text=self.config["hotkey"],
        )
        if not ok:
            return
        hotkey_str = (text or "").strip().lower()
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
        self.ollama_available = self._check_ollama()
        if not self.ollama_available:
            self.cleanup_enabled = False
        self._set_status("Bereit")
        self._notify(
            "IQspeakr - Modell geladen",
            f"Whisper '{self.config['whisper_model']}' ist bereit.\n"
            f"{self.hotkey_label} gedrueckt halten = Aufnahme\n"
            f"{self.hotkey_label} 2x tippen = Daueraufnahme",
        )

    def _check_ollama(self):
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _cleanup_text(self, text):
        if not self.cleanup_enabled or not self.ollama_available:
            return text
        try:
            payload = json.dumps({
                "model": self.config["ollama_model"],
                "prompt": CLEANUP_PROMPT.format(text=text),
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
        if not self.ollama_available:
            self.ollama_available = self._check_ollama()
            if not self.ollama_available:
                self._notify("IQspeakr", "Ollama laeuft nicht. Starte Ollama zuerst.")
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

    def _on_key_press(self, key):
        if self._suppress_listener:
            return
        try:
            if self._single_modifier_mode:
                matcher = self.hotkey_matchers[0]
                if self._key_matches(key, matcher):
                    if not self._hold_mode and (key not in self._pressed_keys):
                        self._pressed_keys.add(key)
                        self._handle_modifier_press()
                    else:
                        self._pressed_keys.add(key)
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
            if self._single_modifier_mode:
                matcher = self.hotkey_matchers[0]
                if self._key_matches(key, matcher):
                    self._pressed_keys.discard(key)
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
        self.overlay.set_recording(False)
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
        self.overlay.set_recording(True)

    def _stop_recording(self):
        self.recording = False  # Audio-Callback hoert sofort auf zu sammeln
        self._set_icon_state("busy")
        log.info(f"Aufnahme gestoppt, {len(self.audio_frames)} Frames aufgenommen")
        self._refresh_menu()
        self.overlay.set_recording(False)

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
        time.sleep(0.3)
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

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("System-Tray nicht verfuegbar - App kann nicht laufen.")
        QMessageBox.critical(None, "IQspeakr",
                             "Das System-Tray ist nicht verfuegbar.")
        sys.exit(1)

    app = IQspeakrApp(qapp)
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
