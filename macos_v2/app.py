#!/usr/bin/env python3
"""
IQspeakr - Lokale Sprache-zu-Text App fuer macOS (v2)
(Qt-basiert: PySide6 QSystemTrayIcon + QWidget-Overlay.)

Portiert aus windows/app.py. Plattform-spezifische Teile (Singleton-Lock,
PATH-Setup, Paste-Key, Dock-Icon) sind Mac-nativ. STT nutzt openai-whisper
mit automatischer MPS-Auswahl (Apple-Silicon-GPU).
"""

import os

# ffmpeg-PATH (openai-whisper braucht ffmpeg fuer manche Dateiformate nicht,
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
import urllib.request
import logging
import time as _time

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

# faulthandler liefert bei C-Level-Crashes Python-Frame + Thread-Dump.
# Nuetzlich zur Diagnose von CoreAudio-/Torch-Problemen.
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
    # Lock erworben - PID reinschreiben (informativ fuer `ps`/Debugging).
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except SystemExit:
    raise
except Exception as _e:
    log.warning(f"Singleton-Check fehlgeschlagen: {_e}")

# Schwere Imports erst NACH dem Singleton-Check.
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
#  darwin.py:140). macOS 26 enforced main-thread-only fuer TSM/Carbon —
#  Listener-Thread crasht mit dispatch_assert_queue_fail.
#
#  Fix: wir rufen keycode_context() genau einmal auf dem Main-Thread beim
#  Import auf und cachen das Ergebnis. Dann monkey-patchen wir pynput,
#  sodass der Listener-Thread die gecachte Version bekommt. Keyboard-
#  Layout-Wechsel waehrend der Session werden dadurch ignoriert — fuer
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

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget,
    QMessageBox, QInputDialog, QMainWindow,
    QDialog, QLabel, QVBoxLayout, QDialogButtonBox,
)
from PySide6.QtGui import (
    QIcon, QPixmap, QAction, QActionGroup, QPainter, QColor,
    QGuiApplication,
)

# Dock-Icon-Handling: LSUIElement=true im Info.plist ist die einzige zuverlaessige
# Methode auf macOS 14+. setActivationPolicy_() via PyObjC zur Laufzeit kann mit
# QSystemTrayIcon kollidieren (Icon erscheint nicht).

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
    # Overlay ist IMMER sichtbar. Im Idle dezent (niedrige Opacity),
    # waehrend Aufnahme kraeftig. Der User wollte kein Auftauchen/
    # Verschwinden-Blitz, sondern eine ruhige dauerhafte Anwesenheit.
    IDLE_ALPHA = 0.22       # dezent, aber sichtbar
    ACTIVE_ALPHA = 0.92     # deutlich, waehrend Aufnahme
    # Abstand zum unteren Bildschirmrand. availableGeometry() respektiert
    # Dock+Menubar — 16px darueber sind genug, sonst schwebt die Pille zu hoch.
    MARGIN_BOTTOM = 16

    def __init__(self, enabled=True):
        # Qt.Tool + WindowStaysOnTop + FramelessWindowHint ist die robuste
        # Mac-Kombi fuer ein Overlay-Widget. WindowDoesNotAcceptFocus
        # sorgt dafuer, dass das aktive Fenster des Users nicht deaktiviert
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
        self._idle_phase = 0.0  # fuer das dezente Idle-Atmen

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
        self._timer.start()

        # Sofort sichtbar mit niedriger Opacity. WA_ShowWithoutActivating
        # verhindert den Fokus-Klau beim initialen show().
        self.setWindowOpacity(0.0)
        self.show()

        # macOS-native Tricks: NSWindow-Level auf Status-Level anheben und
        # collectionBehavior so setzen, dass die Pille ueber ALLEN Apps und
        # auf ALLEN Spaces sichtbar bleibt — unabhaengig davon, ob IQspeakr
        # selbst gerade frontmost ist. Ohne das ist Qt.Tool auf Mac auf
        # "nur sichtbar wenn App aktiv" beschraenkt (Wispr-Flow-Verhalten).
        self._apply_macos_overlay_level()

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
            # Via objc.objc_object(c_void_p=...) zurueck in ein Python-Objekt
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
        # Opacity langsam auf Ziel faden (Active = 0.92, Idle = 0.22).
        diff = self._target_alpha - self._current_alpha
        if abs(diff) > 0.005:
            self._current_alpha += diff * 0.2

        # Im Idle: leichtes Atmen (+/- 0.04) damit das Overlay "lebt".
        if not self._active:
            import math
            self._idle_phase += 0.05
            breath = 0.04 * math.sin(self._idle_phase)
            effective_alpha = max(0.0, min(1.0, self._current_alpha + breath))
        else:
            effective_alpha = self._current_alpha

        self.setWindowOpacity(effective_alpha)

        # Balken abfallen lassen (Decay). Im Idle sind alle 0, im Active
        # werden sie vom Audio-Callback via set_levels regelmaessig neu
        # gefuellt.
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
        """Main-Thread-only. Switched zwischen Idle (dezent) und Aufnahme
        (deutlich + Waveform). Overlay bleibt in beiden Zustaenden sichtbar,
        nur die Opacity faded."""
        if not self.enabled:
            return
        self._active = bool(on)
        if on:
            # Falls User inzwischen Monitor gewechselt hat, Pille
            # neu positionieren.
            self._move_to_primary_screen()
            self._target_alpha = self.ACTIVE_ALPHA
        else:
            self._levels = [0.0] * self.BAR_COUNT
            self._target_alpha = self.IDLE_ALPHA


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

# Qt-Key -> interner Hotkey-String (fuer Custom-Hotkey-Recorder)
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
            # sind Worte. Der Join haengt davon ab, ob nur Symbole im Spiel
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
    """True wenn die Kombi ausschliesslich aus Modifiern besteht (z.B. 'ctrl',
    'ctrl+shift'). Nur dann sind Hold/Tap/Double-Tap sinnvoll - bei Kombis mit
    Buchstaben oder F-Tasten gibt's naemlich keine klare 'lang gehalten'-Semantik."""
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


# --- Tray-Icon-Helfer (Qt rendert Emoji als PNG statt Plain-Kreis) ---

# Mikrofon/Rec/Busy-Emoji aus der System-Emoji-Font in ein PNG rendern.
# Grund: die alte Mac-Version zeigte 🎤 als Menubar-Title; QSystemTrayIcon
# braucht aber ein Bild, kein Text. Qt kann Emojis ueber QFont rendern —
# sieht in der Menubar fast identisch aus zu rumps' Text-Title.
from PySide6.QtGui import QFont
from PySide6.QtCore import QRectF

_ICON_EMOJIS = {
    "ready": "🎤",
    "rec":   "🔴",
    "busy":  "⏳",
}


# =====================================================================
#  NativeStatusBar — Ersatz fuer QSystemTrayIcon.
#
#  Qts QSystemTrayIcon-Cocoa-Backend rendert auf macOS 26 das Icon nicht
#  zuverlaessig (Qt meldet visible=True, trotzdem bleibt die Menubar
#  leer). Wir umgehen Qt komplett und bauen direkt auf NSStatusBar +
#  NSStatusItem + NSMenu — derselbe Mechanismus den rumps in v1 nutzt
#  und der sicher funktioniert.
#
#  Menue-Klicks werden an die bestehenden QAction-Objekte (aus _build_menu)
#  weitergereicht; das Gros der Qt-Struktur bleibt erhalten.
# =====================================================================
_NATIVE_ICON_TITLES = {"ready": "🎤", "rec": "🔴", "busy": "⏳"}


try:
    from Foundation import NSObject
    import objc as _objc

    class _NativeMenuTarget(NSObject):
        """NSObject-Target fuer NSMenuItem-Klicks. Ruft ein Python-
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
# Hybrid-Event-Loops (Qt + CFRunLoop) werden manchmal zu frueh released,
# selbst mit instance-attribute-ref — diese global haelt sie bombenfest.
_GLOBAL_TRAY_REF = None


class NativeStatusBar(QObject):
    """QSystemTrayIcon-Ersatz via Subprocess.

    Qt-hostetes NSStatusItem rendert auf macOS 26 das Menubar-Icon nicht
    (Isoliert-Test funktioniert, Qt-Variante nicht — Qts NSApplication
    blockt den Registrierungspfad). Wir spawnen daher einen reinen
    pyobjc-Kind-Prozess der NUR das Tray hostet. Kommunikation per
    JSON-Lines ueber stdin/stdout.

    Menu-Klicks kommen ueber stdout zurueck und werden via Qt-Signal im
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
    nur lueckenhaft (~3% der Pixel); die native AppKit-Text-Pipeline
    funktioniert dagegen zuverlaessig — das ist der gleiche Weg, den
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

        # 22pt logisch — macOS-Menubar-Standardhoehe. NSImage bekommt
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
        # Fallback: schlichtes graues Quadrat, damit ueberhaupt WAS da ist
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


# =====================================================================
#  Hotkey-Recorder: Dialog, der Tastendruecke live aufnimmt.
# =====================================================================

class HotkeyRecorderDialog(QDialog):
    """Modaler Dialog zum Aufnehmen einer Tastenkombination.

    Der User druckt einfach die gewuenschte Kombi (z.B. ⌃⇧), laesst los,
    der Dialog zeigt das Ergebnis und speichert es beim Klick auf
    "Uebernehmen". Deutlich zuverlaessiger als eine Text-Eingabe (kein
    Layout-/Rechtschreib-Problem, keine ungueltigen Namen)."""

    _MOD_ORDER = ("ctrl", "alt", "shift", "cmd")

    def __init__(self, parent=None, initial=""):
        super().__init__(parent)
        self.setWindowTitle("Eigene Tastenkombination")
        self.setModal(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.resize(440, 190)

        self._mods = set()
        self._key = None
        self._capture_complete = False
        self._captured = (initial or "").strip().lower()

        layout = QVBoxLayout(self)
        info = QLabel(
            "Druecke die gewuenschte Tastenkombination und lass sie los.\n"
            "Dann auf „Uebernehmen“ klicken.\n\n"
            "Erlaubt: Modifier (⌃ ctrl, ⇧ shift, ⌥ alt, ⌘ cmd) + optional ein "
            "Buchstabe, Leertaste, Eingabe, Tab oder F1-F12."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self._display = QLabel()
        font = self._display.font()
        font.setPointSize(16)
        font.setBold(True)
        self._display.setFont(font)
        self._display.setAlignment(Qt.AlignCenter)
        self._display.setMinimumHeight(40)
        layout.addWidget(self._display)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_btn = buttons.button(QDialogButtonBox.Ok)
        self._ok_btn.setText("Uebernehmen")
        buttons.button(QDialogButtonBox.Cancel).setText("Abbrechen")
        # Kein AutoDefault - sonst triggert Enter/Space den OK-Button statt
        # als Taste erkannt zu werden.
        for b in (self._ok_btn, buttons.button(QDialogButtonBox.Cancel)):
            b.setAutoDefault(False)
            b.setDefault(False)
            b.setFocusPolicy(Qt.NoFocus)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_display()

    def _build_combo_str(self):
        parts = [m for m in self._MOD_ORDER if m in self._mods]
        if self._key:
            parts.append(self._key)
        return "+".join(parts)

    def _refresh_display(self):
        live = self._build_combo_str()
        shown = live or self._captured
        if shown:
            self._display.setText(hotkey_display(shown))
        else:
            self._display.setText("(druecke eine Taste)")
        self._ok_btn.setEnabled(bool(self._captured))

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
        # PySide6 6.11+ gibt KeyboardModifier-Enum zurueck, nicht mehr int-kompatibel.
        # .value liefert den darunterliegenden int.
        mods = QApplication.keyboardModifiers()
        m = int(mods.value) if hasattr(mods, "value") else int(mods)
        still_mod_down = bool(
            m & int(Qt.ControlModifier.value) | m & int(Qt.ShiftModifier.value)
            | m & int(Qt.AltModifier.value) | m & int(Qt.MetaModifier.value)
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
#  Haupt-App: QObject mit Signals fuer thread-safe GUI-Updates.
# =====================================================================

class IQspeakrApp(QObject):

    # Signals, die Worker-Threads emittieren koennen, um die GUI
    # (tray-icon, menue, notifications) im Main-Thread zu aktualisieren.
    icon_state_sig = Signal(str)
    rebuild_menu_sig = Signal()
    notify_sig = Signal(str, str)
    status_sig = Signal(str)
    # Overlay-Show/Hide MUSS ueber Signal laufen, nicht direkt. Qt-Widgets
    # duerfen nur vom Main-Thread erstellt/sichtbar gemacht werden — aus
    # dem pynput-Listener-Callback (CGEventTap-Thread) direkt aufgerufen
    # crasht das mit SIGABRT in NSWindow-Init.
    overlay_recording_sig = Signal(bool)

    def __init__(self, qapp):
        super().__init__()
        log.info("=" * 60)
        log.info(f"IQspeakr startet (PID {os.getpid()}, frozen={getattr(sys, 'frozen', False)})")
        self.qapp = qapp
        self.config = load_config()
        log.info(
            f"Config: hotkey={self.config.get('hotkey')!r}, "
            f"whisper={self.config.get('whisper_model')!r}, "
            f"lang={self.config.get('language')!r}, "
            f"overlay={self.config.get('overlay_enabled')}"
        )
        self.recording = False
        self.audio_frames = []
        # Persistenter Audio-Stream: einmal geoeffnet, lebt bis zum Quit.
        # Vermeidet PortAudio-Races bei rapidem open/close. CoreAudio-
        # Stop/Close laeuft beim Quit im Hintergrund-Thread (Mac-Deadlock-
        # Regel: sd.InputStream.stop()/close() darf nicht im Main-Thread
        # waehrend eines Callbacks laufen).
        self._persistent_stream = None
        # Sperrt Listener kurzzeitig waehrend wir Cmd+V simulieren - sonst
        # sieht pynput die simulierten Keys als Hotkey-Press (Self-Trigger).
        self._suppress_listener = False

        # Pill-Overlay (QWidget) - im Main-Thread erzeugt, thread-safe via
        # Signals. KEIN show() hier - Overlay zeigt sich erst bei
        # set_recording(True), sonst haengt es permanent transparent am
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

        # Tray-Icon ueber natives NSStatusItem (wie v1/rumps).
        # WICHTIG: Creation muss NACH dem Start der Qt-Event-Loop laufen,
        # sonst ueberschreibt Qts NSApplication-Init den Status-Item-
        # Registrierungspfad auf macOS 26 (Icon wird nie gerendert).
        self.tray = None  # placeholder; wird in _init_native_tray befuellt
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

        # Whisper-Modell laden (Thread)
        threading.Thread(target=self._load_model, daemon=True).start()

        # Global Hotkey-Listener (pynput). Benoetigt Bedienungshilfen-
        # Berechtigung auf macOS (Systemeinstellungen -> Datenschutz ->
        # Bedienungshilfen). Beim ersten Start kommt ein System-Prompt.
        # Ohne Permission schmeisst pynput intern (im Listener-Thread) —
        # deshalb brauchen wir einen Watchdog, der den Listener-Zustand
        # kurz nach dem Start prueft.
        self._listener = None
        self._start_hotkey_listener()
        # Watchdog: nach 1.5s pruefen, ob der Listener-Thread noch laeuft.
        # pynput stirbt bei fehlender Accessibility-Permission ggf. leise
        # (Objective-C Exception im Hintergrund-Thread).
        QTimer.singleShot(1500, self._check_listener_health)

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
            # dem User eine klare Fehlermeldung und oeffnen den
            # System-Einstellungen-Dialog.
            log.exception(f"Hotkey-Listener konnte nicht starten: {e}")
            self._set_status("Hotkey deaktiviert (Accessibility?)")
            QTimer.singleShot(500, self._show_accessibility_hint)

    def _check_listener_health(self):
        """Prueft ob der pynput-Listener wirklich laeuft. pynput startet zwar
        den Thread, der Event-Tap kann aber trotzdem stillschweigend fehlen,
        wenn macOS die Accessibility-Permission nicht erteilt hat."""
        try:
            running = bool(self._listener and self._listener.running)
            alive = bool(self._listener and self._listener.is_alive())
        except Exception:
            running, alive = False, False
        if not (running and alive):
            log.warning(
                f"Listener-Watchdog: running={running} alive={alive} -> "
                "vermutlich fehlt die Bedienungshilfen-Berechtigung."
            )
            self._set_status("Hotkey deaktiviert (Accessibility?)")
            self._show_accessibility_hint()
        else:
            log.info("Listener-Watchdog: Hotkey-Erkennung laeuft sauber.")

    def _show_accessibility_hint(self):
        """Einmalige Hinweisdialog + Oeffnen der System-Einstellungen.
        Wird nur bei fehlender Accessibility-Permission aufgerufen."""
        if getattr(self, "_accessibility_hint_shown", False):
            return
        self._accessibility_hint_shown = True
        self._notify(
            "IQspeakr - Bedienungshilfen noetig",
            "Bitte IQspeakr in Systemeinstellungen > Datenschutz "
            "> Bedienungshilfen aktivieren, dann App neu starten.",
        )
        # System-Einstellungen direkt auf den richtigen Pane oeffnen.
        try:
            subprocess.Popen([
                "open",
                "x-apple.systempreferences:com.apple.preference.security"
                "?Privacy_Accessibility",
            ])
        except Exception as e:
            log.warning(f"System-Einstellungen konnten nicht geoeffnet werden: {e}")

    # --- Slots (laufen immer im Main-Thread) ---

    def _on_icon_state(self, state):
        if self.tray is None:
            return  # Tray wird deferred initialisiert — Zustand holt sich der
                    # naechste Aufruf nach Init.
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
        hotkey_options = ["ctrl+shift", "ctrl", "shift", "alt", "cmd"]
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
        # Ende raeumt ihn trotzdem sauber ab (daemon).
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
            # error: Aborted). Opt-in moeglich via config.json
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

        # Persistenten Audio-Stream oeffnen - bleibt bis zum Quit aktiv.
        # WICHTIG: Nur EINMAL oeffnen. Bei Modell-Wechsel wird _load_model
        # erneut gestartet — der bestehende Stream darf dann nicht ueber-
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
                "IQspeakr - Mikrofon noetig",
                "Mikrofon-Zugriff fehlt oder kein Mikro gefunden. "
                "Systemeinstellungen > Datenschutz > Mikrofon pruefen, "
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
                # openai-whisper nimmt das numpy-Array direkt — kein tempfile
                # noetig. fp16=False, weil MPS in manchen Torch-Versionen
                # fp16-Probleme hat. beam_size=1 und condition_on_previous_text
                # analog zur Windows-Version fuer Speed.
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
        """Simuliert Cmd+V (Mac-Paste). Sperrt waehrenddessen den eigenen
        pynput-Listener, damit der die simulierten Keys nicht als Hotkey-
        Press missdeutet (Self-Trigger-Bug)."""
        import time
        # Mini-Delay, damit das OS den Hotkey-Key-Up sauber verarbeitet hat
        # bevor wir Cmd+V simulieren. 50 ms sind spuerbar schneller als die
        # frueheren 300 ms und reichen in der Praxis.
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
    # multiprocessing.freeze_support ist auf macOS nicht zwingend
    # (spawn-Start statt fork), kostet aber nix — mitnehmen fuer
    # py2app-Bundles.
    import multiprocessing
    multiprocessing.freeze_support()

    log.info(f"main() start - Python {sys.version.split()[0]}")

    qapp = QApplication(sys.argv)
    # Verhindert dass das Schliessen des (unsichtbaren) Overlay-Fensters
    # die gesamte App beendet.
    qapp.setQuitOnLastWindowClosed(False)
    # Kein _hide_dock_icon()-Aufruf mehr: LSUIElement=true im Info.plist
    # erledigt das bereits. Ein zusaetzliches setActivationPolicy(Accessory)
    # zu frueh verhindert auf macOS 14+ sporadisch das Anzeigen des
    # QSystemTrayIcon (Menubar-Icon "fehlt").

    # Systemtray-Check entfallen: wir nutzen NSStatusBar direkt und nicht
    # mehr QSystemTrayIcon. NSStatusBar.systemStatusBar() ist immer da.

    try:
        app = IQspeakrApp(qapp)
    except Exception:
        log.exception("FATAL: IQspeakrApp-Init gescheitert")
        QMessageBox.critical(
            None, "IQspeakr",
            "App konnte nicht starten. Siehe ~/IQspeakr.log.",
        )
        sys.exit(1)

    log.info("Qt Event-Loop uebernimmt (qapp.exec)...")
    # Referenz auf `app` am Leben halten, sonst GC's Qt-Tray-Icon weg.
    qapp._iqspeakr_app = app
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
