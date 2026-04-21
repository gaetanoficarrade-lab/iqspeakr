#!/usr/bin/env python3
"""
IQspeakr — Lokale Sprache-zu-Text App für Windows
(Portiert aus der macOS-Version, 1:1 Feature-Parität)
"""

import threading
import subprocess
import tempfile
import os
import wave
import json
import urllib.request
import logging
import sys
import time as _time
from pathlib import Path

# ffmpeg muss im PATH sein für Whisper.
# Auf Windows liegt die optional mitgelieferte ffmpeg.exe in ~/IQspeakr/bin.
# (Homebrew-Pfade entfallen; os.pathsep statt hartkodiertem ":".)
_IQSPEAKR_BIN = str(Path.home() / "IQspeakr" / "bin")
if _IQSPEAKR_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _IQSPEAKR_BIN + os.pathsep + os.environ.get("PATH", "")

logging.basicConfig(
    filename=str(Path.home() / "IQspeakr.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("IQspeakr")
log.addHandler(logging.StreamHandler(sys.stderr))

# --- Singleton: nur eine Instanz erlauben ---
# Verhindert, dass mehrfaches Starten von IQspeakr.exe (z.B. schneller
# Doppel- oder Dreifach-Klick auf das Taskleisten-Icon) zu doppelten/
# dreifachen Paste-Events fuehrt — jede Instanz registriert einen eigenen
# globalen Hotkey-Listener und sendet Ctrl+V.
#
# Windows hat kein fcntl — stattdessen msvcrt.locking() mit LK_NBLCK
# (non-blocking exclusive lock auf 1 Byte). Verhalten analog zu flock:
# Nur ein Prozess bekommt den Lock, alle weiteren scheitern sofort mit
# OSError. Der Lock wird vom Kernel bei Prozess-Ende automatisch frei-
# gegeben — auch bei harten Kills oder Crash.
import msvcrt

_LOCK_FILE = os.path.join(tempfile.gettempdir(), "iqspeakr.lock")
try:
    # WICHTIG: Modul-Global behalten, sonst schliesst der GC den FD und
    # gibt den Lock frei.
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        # Mindestens 1 Byte schreiben — msvcrt.locking braucht eine
        # Byte-Region zum Sperren.
        _lock_fd.write(" ")
        _lock_fd.flush()
        _lock_fd.seek(0)
        msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.info("IQspeakr laeuft bereits — zweite Instanz beendet sich.")
        sys.exit(0)
    # Lock erworben — PID reinschreiben (informativ fuer Task-Manager / Debugging).
    _lock_fd.seek(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except SystemExit:
    raise
except Exception as _e:
    log.warning(f"Singleton-Check fehlgeschlagen: {_e}")

# Erst NACH dem Singleton-Check die schweren Imports — sonst laedt jede
# geblockte Zweitinstanz trotzdem numpy/whisper/pystray (mehrere Sekunden).
import numpy as np
import sounddevice as sd
import whisper
import pystray
from pystray import Menu, MenuItem
from PIL import Image, ImageDraw
import pyperclip
from pynput import keyboard
from pynput.keyboard import Key, KeyCode, Controller
import tkinter as tk
from tkinter import messagebox, simpledialog

# --- Pfade ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

OLLAMA_URL = "http://localhost:11434/api/generate"
SAMPLE_RATE = 16000

CLEANUP_PROMPT = """Du bereinigst gesprochene Sprache minimal-invasiv. WICHTIG: Du DARFST den Text NICHT umformulieren oder paraphrasieren. Der Sprecher soll seinen eigenen Stil wiedererkennen.

ERLAUBT:
- Füllwörter entfernen (ähm, äh, also, sozusagen, halt, quasi, irgendwie, eben, ja, nun)
- Wortdoppelungen und Stotterer entfernen (z.B. "ich ich habe" → "ich habe")
- Satzzeichen und Großschreibung korrigieren
- Offensichtliche Grammatikfehler korrigieren (z.B. falsche Artikel, Kasus)
- Kleine Satzumstellungen NUR wenn grammatikalisch notwendig

VERBOTEN:
- Wörter durch Synonyme ersetzen
- Sätze neu formulieren oder glätten
- Inhalt straffen oder zusammenfassen
- Stil verändern (umgangssprachlich → schriftsprachlich)
- Eigene Wörter hinzufügen

Antworte NUR mit dem bereinigten Text, ohne Erklärungen, ohne Anführungszeichen.

Text: {text}"""

# --- Hotkey ---
# Mapping Hotkey-Name → pynput-Key-Objekt(e).
# Fuer Modifier-Keys gibt es jeweils die generische Form (Key.ctrl) und
# die links/rechts-Varianten; wir akzeptieren beim Matching alle drei.
_MODIFIER_ALIASES = {
    "ctrl":    {Key.ctrl, Key.ctrl_l, Key.ctrl_r},
    "control": {Key.ctrl, Key.ctrl_l, Key.ctrl_r},
    "shift":   {Key.shift, Key.shift_l, Key.shift_r},
    "alt":     {Key.alt, Key.alt_l, Key.alt_r},
    "option":  {Key.alt, Key.alt_l, Key.alt_r},
    # pynput nennt die Windows-Taste auf Windows "cmd" (Super).
    "cmd":     {Key.cmd, Key.cmd_l, Key.cmd_r},
    "command": {Key.cmd, Key.cmd_l, Key.cmd_r},
    "win":     {Key.cmd, Key.cmd_l, Key.cmd_r},
}

# Nicht-Modifier-Namen → einzelnes Key-Objekt.
_NAMED_KEYS = {
    "space": Key.space,
    "enter": Key.enter,
    "tab":   Key.tab,
    "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
    "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
    "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
}

# Fuer die Menueleisten-Anzeige — Windows-Text statt macOS-Symbolen.
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
    """Parst 'ctrl+space' -> Liste von 'Matchern'.

    Ein Matcher ist entweder ein set() von Key-Objekten (Modifier mit
    L/R-Varianten) oder ein einzelnes Key/KeyCode-Objekt. Die Logik unten
    vergleicht gedrueckte Tasten gegen diese Matcher.
    """
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
        else:
            # Unbekannter Token — ignoriert, liefert ggf. leere Liste zurueck.
            pass
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
    """True, wenn der Hotkey aus genau einem Modifier besteht (z.B. 'ctrl').
    Nur dann schaltet sich die Hold/Tap/Double-Tap-State-Machine aktiv."""
    parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
    return len(parts) == 1 and _is_modifier_name(parts[0])


def _whisper_model_cached(size):
    # ~/.cache/whisper/{size}.pt funktioniert cross-platform via Path.home().
    return (Path.home() / ".cache" / "whisper" / f"{size}.pt").is_file()


# --- Config ---
DEFAULT_CONFIG = {
    "hotkey": "ctrl",
    "whisper_model": "small",
    "ollama_model": "llama3.2",
    "cleanup_enabled": True,
    "language": None,
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


# --- Tray-Icon-Helfer ---

def _make_icon_image(state):
    """Erzeugt ein 64x64 Icon mit farblichem Statuskreis.

    state ∈ {'ready', 'rec', 'busy'}.
    """
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = {
        "ready": (80, 80, 80, 255),    # grau
        "rec":   (220, 40, 40, 255),   # rot
        "busy":  (230, 140, 30, 255),  # orange
    }.get(state, (80, 80, 80, 255))
    # Ausgefuellter Kreis mit duennem dunklem Rand fuer Kontrast.
    draw.ellipse((6, 6, 58, 58), fill=color, outline=(0, 0, 0, 255), width=2)
    return img


def _tk_messagebox_askokcancel(title, message):
    """Modaler Ja/Nein-Dialog, frisch erstellte + zerstoerte Tk-Root."""
    root = tk.Tk()
    root.withdraw()
    try:
        return bool(messagebox.askokcancel(title, message, parent=root))
    finally:
        root.destroy()


def _tk_simpledialog_askstring(title, prompt, initialvalue=""):
    root = tk.Tk()
    root.withdraw()
    try:
        return simpledialog.askstring(title, prompt, initialvalue=initialvalue, parent=root)
    finally:
        root.destroy()


class IQspeakrApp:
    def __init__(self):
        self.config = load_config()
        self.recording = False
        self.audio_frames = []
        self.stream = None
        # Serialisiert Stream-Ops. Auf macOS wegen CoreAudio-Deadlock Pflicht;
        # auf Windows unschaedlich, aber als Portability-/Robustheits-Muster
        # beibehalten.
        self._stream_lock = threading.Lock()
        self.model = None
        self.hotkey_matchers = parse_hotkey(self.config["hotkey"])
        self.hotkey_label = hotkey_display(self.config["hotkey"])
        self._single_modifier_mode = _hotkey_is_single_modifier(self.config["hotkey"])

        # Zustand fuer Hold/Tap/Double-Tap-State-Machine
        self._ctrl_press_time = 0
        self._last_tap_time = 0
        self._continuous_mode = False
        self._hold_mode = False
        self._hold_threshold = 0.3
        self._double_tap_window = 0.4

        # Fuer den 2. Hotkey-Modus (Kombination wie ctrl+space): Set der
        # aktuell gedrueckten Tasten, und Flag ob die Kombo bereits aktiv war.
        self._pressed_keys = set()
        self._combo_active = False

        # Key-Controller fuer Ctrl+V-Simulation beim Einfuegen.
        self._kb_controller = Controller()

        self.cleanup_enabled = self.config["cleanup_enabled"]
        self.ollama_available = False

        # --- pystray Menue-Bau ---
        self.icon = pystray.Icon(
            "IQspeakr",
            icon=_make_icon_image("ready"),
            title="IQspeakr",
            menu=self._build_menu(),
        )

        self._set_status("Modell wird geladen...")
        threading.Thread(target=self._load_model, daemon=True).start()

        # Globaler Hotkey-Listener (pynput).
        self._listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self._listener.daemon = True
        self._listener.start()
        log.info("pynput Keyboard-Listener aktiv — Hotkey-Erkennung laeuft")

    # --- Menue-Bau ---

    def _build_menu(self):
        """Baut das pystray-Menue neu auf.

        pystray-Menues sind statisch (Callbacks koennen sich nicht einfach
        im Nachhinein aendern), daher bauen wir die Menue-Struktur komplett
        als Lambda-/Closure-Kette und setzen sie via self.icon.menu neu,
        wenn sich Config-State aendert.
        """

        # Hotkey-Submenue
        hotkey_options = ["ctrl", "cmd", "alt", "shift"]
        hotkey_items = []
        for opt in hotkey_options:
            label = hotkey_display(opt)
            hotkey_items.append(
                MenuItem(
                    f"{label} halten  ({opt})",
                    self._make_hotkey_callback(opt),
                    checked=self._checker_eq("hotkey", opt),
                    radio=True,
                )
            )
        hotkey_items.append(Menu.SEPARATOR)
        hotkey_items.append(MenuItem("Eigene Kombination...", self._custom_hotkey))
        hotkey_menu = Menu(*hotkey_items)

        # Whisper-Submenue
        whisper_options = [
            ("tiny", "tiny — Sehr schnell, ungenauer (~75 MB)"),
            ("base", "base — Guter Kompromiss (~145 MB)"),
            ("small", "small — Gute Qualität (~465 MB)"),
            ("medium", "medium — Empfohlen, beste Qualität (~1.5 GB)"),
        ]
        whisper_items = [
            MenuItem(
                label,
                self._make_whisper_callback(size),
                checked=self._checker_eq("whisper_model", size),
                radio=True,
            )
            for size, label in whisper_options
        ]
        whisper_menu = Menu(*whisper_items)

        # Ollama-Submenue
        ollama_options = [
            ("llama3.2", "llama3.2 — Klein & schnell (3B)"),
            ("llama3.1", "llama3.1 — Bessere Qualität (8B)"),
            ("mistral", "mistral — Gut für Deutsch/Europäisch (7B)"),
            ("gemma2", "gemma2 — Google, solide Qualität (9B)"),
            ("phi3", "phi3 — Microsoft, kompakt & gut (3.8B)"),
        ]
        ollama_items = [
            MenuItem(
                label,
                self._make_ollama_callback(m),
                checked=self._checker_eq("ollama_model", m),
                radio=True,
            )
            for m, label in ollama_options
        ]
        ollama_menu = Menu(*ollama_items)

        # Sprache-Submenue
        lang_options = [
            (None, "Automatisch"),
            ("de", "Deutsch"),
            ("en", "English"),
            ("fr", "Français"),
            ("es", "Español"),
            ("it", "Italiano"),
        ]
        lang_items = [
            MenuItem(
                label,
                self._make_lang_callback(code),
                checked=self._checker_eq("language", code),
                radio=True,
            )
            for code, label in lang_options
        ]
        lang_menu = Menu(*lang_items)

        settings_menu = Menu(
            MenuItem("Tastenkombination", hotkey_menu),
            MenuItem("Whisper-Modell (Spracherkennung)", whisper_menu),
            MenuItem("Ollama-Modell (Textbereinigung)", ollama_menu),
            MenuItem("Sprache", lang_menu),
        )

        return Menu(
            MenuItem(
                lambda _i: f"Aufnahme starten ({self.hotkey_label} halten)"
                           if not self.recording
                           else f"Aufnahme stoppen ({self.hotkey_label} loslassen)",
                self._menu_toggle_recording,
            ),
            Menu.SEPARATOR,
            MenuItem(
                lambda _i: f"KI-Bereinigung: {'An' if self.cleanup_enabled else ('Aus (Ollama nicht gefunden)' if not self.ollama_available else 'Aus')}",
                self._menu_toggle_cleanup,
            ),
            MenuItem("Einstellungen", settings_menu),
            Menu.SEPARATOR,
            MenuItem(
                lambda _i: f"Status: {self._status_text}",
                None,
                enabled=False,
            ),
            MenuItem("Konfig-Datei oeffnen", self._menu_open_config),
            Menu.SEPARATOR,
            MenuItem("Beenden", self._menu_quit),
        )

    def _checker_eq(self, config_key, value):
        # pystray erwartet eine Funktion(item)->bool fuer 'checked'.
        return lambda _item: self.config.get(config_key) == value

    def _refresh_menu(self):
        """Menue neu bauen + Icon informieren."""
        self.icon.menu = self._build_menu()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _set_status(self, text):
        self._status_text = text
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _set_icon_state(self, state):
        try:
            self.icon.icon = _make_icon_image(state)
        except Exception:
            pass

    def _notify(self, title, message):
        try:
            self.icon.notify(message=message, title=title)
        except Exception as e:
            log.warning(f"Notification fehlgeschlagen: {e}")

    # --- Menue-Callbacks (pystray) ---

    def _menu_toggle_recording(self, icon, item):
        self.toggle_recording(None)

    def _menu_toggle_cleanup(self, icon, item):
        self.toggle_cleanup(None)

    def _menu_open_config(self, icon, item):
        self.open_config(None)

    def _menu_quit(self, icon, item):
        try:
            self._listener.stop()
        except Exception:
            pass
        self.icon.stop()

    # --- Einstellungen-Callbacks ---

    def _make_hotkey_callback(self, hotkey_str):
        def callback(icon, item):
            self._apply_hotkey(hotkey_str)
        return callback

    def _apply_hotkey(self, hotkey_str):
        self.config["hotkey"] = hotkey_str
        self.hotkey_matchers = parse_hotkey(hotkey_str)
        self.hotkey_label = hotkey_display(hotkey_str)
        self._single_modifier_mode = _hotkey_is_single_modifier(hotkey_str)
        # State-Machine zuruecksetzen, damit alte Zustaende nicht nachwirken.
        self._pressed_keys.clear()
        self._combo_active = False
        self._hold_mode = False
        self._continuous_mode = False
        save_config(self.config)
        self._refresh_menu()
        self._notify("IQspeakr", f"Neuer Hotkey: {self.hotkey_label} halten")

    def _custom_hotkey(self, icon, item):
        # tkinter-Dialog muss im Main-Thread laufen. pystray-Callbacks laufen
        # in einem Worker-Thread auf Windows — wir starten den Dialog also in
        # einem eigenen Thread, der sich seine Tk-Root selbst erzeugt und
        # wieder zerstoert.
        def run_dialog():
            prompt = (
                "Gib deine Kombination ein, z.B.:\n\n"
                "  ctrl+space\n"
                "  alt+shift+r\n"
                "  ctrl+shift+space\n"
                "  f8\n\n"
                "Verfuegbare Tasten: ctrl, alt, shift, win,\n"
                "space, enter, tab, f1-f12, oder ein Buchstabe (a-z)"
            )
            text = _tk_simpledialog_askstring(
                "Eigene Tastenkombination",
                prompt,
                initialvalue=self.config["hotkey"],
            )
            if text is None:
                return
            hotkey_str = text.strip().lower()
            if not hotkey_str:
                return
            matchers = parse_hotkey(hotkey_str)
            if matchers:
                self._apply_hotkey(hotkey_str)
            else:
                self._notify("IQspeakr", f"Ungueltige Kombination: {hotkey_str}")

        threading.Thread(target=run_dialog, daemon=True).start()

    def _make_whisper_callback(self, size):
        def callback(icon, item):
            if size == self.config["whisper_model"]:
                return
            if not _whisper_model_cached(size):
                mb = {"tiny": 75, "base": 145, "small": 465, "medium": 1500}.get(size, 0)
                # Dialog im Main-Thread geht schief, wenn pystray-Callbacks
                # auf eigenem Thread laufen — wir nutzen einfach einen frischen
                # Tk-Root, das klappt aus jedem Thread (nur nicht gleichzeitig).
                ok = _tk_messagebox_askokcancel(
                    f"Whisper-Modell '{size}' herunterladen?",
                    f"Das Modell ist noch nicht auf deinem PC. Es werden ca. {mb} MB heruntergeladen.",
                )
                if not ok:
                    return
            self.config["whisper_model"] = size
            save_config(self.config)
            self.model = None
            self._set_status(f"Lade Whisper '{size}'...")
            self._refresh_menu()
            threading.Thread(target=self._load_model, daemon=True).start()
        return callback

    def _make_ollama_callback(self, model_name):
        def callback(icon, item):
            self.config["ollama_model"] = model_name
            save_config(self.config)
            self._refresh_menu()
            self._notify("IQspeakr", f"Ollama-Modell geaendert: {model_name}")
        return callback

    def _make_lang_callback(self, lang_code):
        def callback(icon, item):
            self.config["language"] = lang_code
            save_config(self.config)
            self._refresh_menu()
            label = "Automatisch" if lang_code is None else lang_code
            self._notify("IQspeakr", f"Sprache geaendert: {label}")
        return callback

    def open_config(self, sender):
        try:
            os.startfile(CONFIG_PATH)  # Windows-eigene "Mit Standard-App oeffnen"
        except Exception as e:
            log.warning(f"open_config: {e}")

    # --- Modell laden ---

    def _load_model(self):
        self.model = whisper.load_model(self.config["whisper_model"])
        # Audio-Stream einmal kurz oeffnen, damit der erste Start sofort klappt
        try:
            s = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32')
            s.start(); s.stop(); s.close()
        except Exception:
            pass
        self.ollama_available = self._check_ollama()
        status = "Bereit"
        if not self.ollama_available:
            self.cleanup_enabled = False
            status = "Bereit (ohne KI-Bereinigung)"
        self._set_status(status)
        self._refresh_menu()
        self._notify(
            "IQspeakr — Modell geladen",
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
                "options": {
                    "temperature": 0.1,  # wenig Kreativitaet → naeher am Originaltext
                    "top_p": 0.5,
                },
            }).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                cleaned = result.get("response", "").strip()
                return cleaned if cleaned else text
        except Exception:
            return text

    def toggle_cleanup(self, sender):
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
        """True, wenn 'key' zum 'matcher' passt (set von Keys oder einzelnes Key)."""
        if isinstance(matcher, set):
            return key in matcher
        # Einzelnes Key / KeyCode.
        if isinstance(matcher, KeyCode) and isinstance(key, KeyCode):
            # Buchstaben vergleichen case-insensitive ueber .char
            a = (matcher.char or "").lower() if matcher.char else None
            b = (key.char or "").lower() if key.char else None
            if a is not None and b is not None:
                return a == b
            return matcher == key
        return key == matcher

    def _on_key_press(self, key):
        try:
            if self._single_modifier_mode:
                # --- Pfad 1: Einzel-Modifier → Hold/Tap/Double-Tap-Logik ---
                matcher = self.hotkey_matchers[0]
                if self._key_matches(key, matcher):
                    # Nur Flanke behandeln (nicht bei Key-Repeat).
                    if not self._hold_mode and (key not in self._pressed_keys):
                        self._pressed_keys.add(key)
                        self._handle_modifier_press()
                    else:
                        # Key-Repeat / bereits gedrueckt — ignorieren.
                        self._pressed_keys.add(key)
                return

            # --- Pfad 2: Kombi-Hotkey (z.B. ctrl+space) ---
            self._pressed_keys.add(key)
            if self._combo_matches() and not self._combo_active:
                self._combo_active = True
                self.toggle_recording(None)
        except Exception as e:
            log.error(f"on_key_press Fehler: {e}", exc_info=True)

    def _on_key_release(self, key):
        try:
            if self._single_modifier_mode:
                matcher = self.hotkey_matchers[0]
                if self._key_matches(key, matcher):
                    # Alle L/R-Varianten entfernen, dann Release-Logik.
                    self._pressed_keys.discard(key)
                    self._handle_modifier_release()
                return

            # Kombi-Hotkey: Key rausnehmen. Sobald die Kombo nicht mehr voll
            # gedrueckt ist, Flag zuruecksetzen (damit der naechste voller
            # Druck wieder triggert).
            self._pressed_keys.discard(key)
            if self._combo_active and not self._combo_matches():
                self._combo_active = False
        except Exception as e:
            log.error(f"on_key_release Fehler: {e}", exc_info=True)

    def _combo_matches(self):
        """True, wenn aktuell jeder Matcher der Hotkey-Kombi durch irgend-
        eine gedrueckte Taste abgedeckt ist."""
        for matcher in self.hotkey_matchers:
            if isinstance(matcher, set):
                if not any(k in matcher for k in self._pressed_keys):
                    return False
            else:
                if not any(self._key_matches(k, matcher) for k in self._pressed_keys):
                    return False
        return True

    # --- State-Machine fuer Einzel-Modifier-Hotkey (Hold/Tap/Double-Tap) ---
    # 1:1 aus der macOS-Version (Zeilen 469-517), nur an pynput-Events
    # angepasst. Timings und Thresholds unveraendert.

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
                log.debug(f"Kurzer Tap ({hold_duration:.2f}s) — warte auf Doppel-Tap")
                self._last_tap_time = now
                self._hold_mode = False
                if self.recording and not self._continuous_mode:
                    self._cancel_recording()
        else:
            if self.recording:
                log.info(f"Hotkey losgelassen nach {hold_duration:.1f}s — transkribiere")
                self._hold_mode = False
                self._stop_recording()

    def _cancel_recording(self):
        self.recording = False
        self._set_icon_state("ready")
        stream = self.stream
        self.stream = None
        self.audio_frames = []
        if stream:
            threading.Thread(target=self._close_stream_async, args=(stream,), daemon=True).start()
        self._refresh_menu()
        log.debug("Aufnahme abgebrochen (zu kurzer Tap)")

    def _close_stream_async(self, stream):
        with self._stream_lock:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                log.warning(f"Stream-Stopp Fehler: {e}")

    def toggle_recording(self, sender):
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
        self.recording = True
        self.audio_frames = []
        self._set_icon_state("rec")
        self._refresh_menu()

        threading.Thread(target=self._start_stream_async, daemon=True).start()

    def _start_stream_async(self):
        def audio_callback(indata, frames, time_info, status):
            if status:
                log.warning(f"Audio-Status: {status}")
            if self.recording:
                self.audio_frames.append(indata.copy())

        with self._stream_lock:
            if not self.recording:
                return
            try:
                stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                    callback=audio_callback,
                )
                stream.start()
                self.stream = stream
                log.info("Audio-Stream gestartet")
            except Exception as e:
                log.error(f"Audio-Stream Fehler: {e}")

    def _stop_recording(self):
        self.recording = False
        self._set_icon_state("busy")
        log.info(f"Aufnahme gestoppt, {len(self.audio_frames)} Frames aufgenommen")
        self._refresh_menu()

        stream = self.stream
        self.stream = None
        frames = self.audio_frames
        self.audio_frames = []

        threading.Thread(
            target=self._stop_and_transcribe,
            args=(stream, frames),
            daemon=True,
        ).start()

    def _stop_and_transcribe(self, stream, frames):
        if stream:
            with self._stream_lock:
                try:
                    stream.stop()
                    stream.close()
                except Exception as e:
                    log.warning(f"Stream-Stopp Fehler: {e}")

        if not frames:
            log.warning("Keine Audio-Frames aufgenommen!")
            self._set_icon_state("ready")
            return

        self.audio_frames = frames
        self._transcribe()

    def _transcribe(self):
        audio_data = np.concatenate(self.audio_frames, axis=0).flatten()
        log.info(f"Transkribiere: {len(audio_data)} Samples, Peak: {np.max(np.abs(audio_data)):.4f}")

        # delete=False weil Whisper die Datei selbst oeffnet. Auf Windows
        # kann man eine noch offene NamedTemporaryFile nicht parallel lesen
        # (anders als unter POSIX), deshalb schliessen wir erst den File-Handle.
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_name = tmp.name
        tmp.close()
        try:
            with wave.open(tmp_name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                audio_int16 = (audio_data * 32767).astype(np.int16)
                wf.writeframes(audio_int16.tobytes())

            lang = self.config.get("language")
            log.info(f"Starte Whisper-Transkription (Sprache: {lang})...")
            try:
                result = self.model.transcribe(tmp_name, language=lang)
                raw_text = result["text"].strip()
                log.info(f"Whisper-Ergebnis: '{raw_text}' (Sprache: {result.get('language', '?')})")
            except Exception as e:
                log.error(f"Whisper-Fehler: {e}")
                self._notify("IQspeakr — Fehler", str(e)[:100])
                return

            if raw_text:
                text = self._cleanup_text(raw_text)
                log.info(f"Bereinigter Text: '{text}'")
                # Clipboard via pyperclip — nutzt unter Windows WinAPI.
                pyperclip.copy(text)
                log.info("Text in Zwischenablage kopiert")
                import time
                time.sleep(0.3)
                # Ctrl+V simulieren statt Cmd+V (macOS).
                try:
                    self._kb_controller.press(Key.ctrl)
                    self._kb_controller.press('v')
                    self._kb_controller.release('v')
                    self._kb_controller.release(Key.ctrl)
                    log.info("Ctrl+V via pynput ausgefuehrt")
                except Exception as e:
                    log.error(f"Paste-Fehler: {e}")
                log.info(f"Eingefuegt: '{text}'")
            else:
                log.warning("Kein Text erkannt")
                self._notify("IQspeakr", "Kein Text erkannt.")
        except Exception as e:
            log.error(f"Transkriptions-Fehler: {e}", exc_info=True)
        finally:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
            self._set_icon_state("ready")
            self._refresh_menu()

    def run(self):
        # pystray.Icon.run() blockiert im Main-Thread — das ist auf Windows
        # Pflicht, weil Shell_NotifyIcon-Messages ueber die Fenster-Message-
        # Loop des erstellenden Threads laufen.
        self.icon.run()


if __name__ == "__main__":
    IQspeakrApp().run()
