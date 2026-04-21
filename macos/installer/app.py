#!/usr/bin/env python3
"""
IQspeakr — Lokale Sprache-zu-Text App für macOS
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

# ffmpeg muss im PATH sein für Whisper
_IQSPEAKR_BIN = os.path.expanduser("~/.iqspeakr/bin")
_PATH_PARTS = [_IQSPEAKR_BIN, "/opt/homebrew/bin", "/usr/local/bin"]
for _p in _PATH_PARTS:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

logging.basicConfig(
    filename=os.path.expanduser("~/IQspeakr.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("IQspeakr")
log.addHandler(logging.StreamHandler(sys.stderr))

# --- Singleton: nur eine Instanz erlauben ---
# Verhindert, dass mehrfaches `open IQspeakr.app` (z.B. schneller Doppel-
# oder Dreifach-Klick auf das Dock-/Launchpad-Icon) zu doppelten/dreifachen
# Paste-Events fuehrt — jede Instanz registriert einen eigenen globalen
# NSEvent-Monitor und sendet Cmd+V.
#
# Frueher: PID-Datei mit os.path.exists + open("w"). Hatte eine TOCTOU-
# Race: Wenn zwei Launcher gleichzeitig starten, liest einer die Datei
# noch leer (oder gar nicht existent), der andere schreibt seine PID —
# beide glauben, sie seien allein, beide starten.
#
# Jetzt: fcntl.flock(LOCK_EX | LOCK_NB). Atomar. Nur ein Prozess bekommt
# den Lock, alle weiteren scheitern sofort mit BlockingIOError. Der Lock
# wird vom Kernel bei Prozess-Ende automatisch freigegeben — auch bei
# kill -9 oder Crash. Keine Leichen-Datei-Aufraeumung noetig.
import fcntl

_LOCK_FILE = "/tmp/iqspeakr.pid"
try:
    # WICHTIG: Die Variable bleibt als Modul-Global bestehen. Sobald der
    # File-Descriptor geschlossen (bzw. vom Garbage Collector eingesammelt)
    # wuerde, gaebe der Kernel den Lock frei. Also nie zumachen.
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("IQspeakr laeuft bereits — zweite Instanz beendet sich.")
        sys.exit(0)
    # Lock erworben — PID reinschreiben (informativ fuer `ps` / Debugging).
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except SystemExit:
    raise
except Exception as _e:
    log.warning(f"Singleton-Check fehlgeschlagen: {_e}")

import numpy as np
import sounddevice as sd
import rumps
import whisper
from AppKit import NSApplication, NSImage, NSEvent, NSKeyDownMask, NSFlagsChangedMask
from Quartz import CGEventCreateKeyboardEvent, CGEventSetFlags, CGEventPost, kCGSessionEventTap, kCGEventFlagMaskCommand

# --- Kein Dock-Icon, nur Menüleiste ---
NSApplication.sharedApplication().setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory

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
KEYCODE_MAP = {
    "ctrl": 59, "control": 59,
    "shift": 56,
    "cmd": 55, "command": 55,
    "alt": 58, "option": 58,
    "fn": 63,
    "space": 49,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

HOTKEY_DISPLAY = {
    "cmd": "⌘", "command": "⌘",
    "shift": "⇧",
    "alt": "⌥", "option": "⌥",
    "ctrl": "⌃", "control": "⌃",
    "fn": "fn",
    "space": "Space",
}

MODIFIER_KEYCODES = {55, 56, 58, 59, 63}


def parse_hotkey(hotkey_str):
    codes = []
    for part in hotkey_str.lower().split("+"):
        part = part.strip()
        if part in KEYCODE_MAP:
            codes.append(KEYCODE_MAP[part])
        elif len(part) == 1:
            codes.append(ord(part.lower()) - ord('a'))
    return codes


def hotkey_display(hotkey_str):
    parts = hotkey_str.lower().split("+")
    display_parts = []
    for part in parts:
        part = part.strip()
        if part in HOTKEY_DISPLAY:
            display_parts.append(HOTKEY_DISPLAY[part])
        else:
            display_parts.append(part.upper())
    return "".join(display_parts)


def _whisper_model_cached(size):
    return os.path.isfile(os.path.expanduser(f"~/.cache/whisper/{size}.pt"))


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
        with open(CONFIG_PATH, "r") as f:
            saved = json.load(f)
            config = DEFAULT_CONFIG.copy()
            config.update(saved)
            return config
    return DEFAULT_CONFIG.copy()


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


class IQspeakrApp(rumps.App):
    def __init__(self):
        super().__init__("IQspeakr", title="🎤")
        self.config = load_config()
        self.recording = False
        self.audio_frames = []
        self.stream = None
        self._stream_lock = threading.Lock()  # Serialisiert alle CoreAudio-Stream-Ops
        self.model = None
        self.current_keys = set()
        self.hotkey = parse_hotkey(self.config["hotkey"])
        self.hotkey_label = hotkey_display(self.config["hotkey"])

        self._ctrl_press_time = 0
        self._last_tap_time = 0
        self._continuous_mode = False
        self._hold_mode = False
        self._hold_threshold = 0.3
        self._double_tap_window = 0.4

        self.cleanup_enabled = self.config["cleanup_enabled"]
        self.ollama_available = False

        self.record_item = rumps.MenuItem(
            f"Aufnahme starten ({self.hotkey_label} halten)", callback=self.toggle_recording
        )
        cleanup_state = "An" if self.cleanup_enabled else "Aus"
        self.cleanup_item = rumps.MenuItem(
            f"KI-Bereinigung: {cleanup_state}", callback=self.toggle_cleanup
        )
        self.status_item = rumps.MenuItem("Status: Bereit")

        self.settings_menu = rumps.MenuItem("Einstellungen")

        self.hotkey_menu = rumps.MenuItem("Tastenkombination")
        hotkey_options = ["ctrl", "cmd", "fn", "shift"]
        for opt in hotkey_options:
            label = hotkey_display(opt)
            item = rumps.MenuItem(f"{label} halten  ({opt})", callback=self._make_hotkey_callback(opt))
            if opt == self.config["hotkey"]:
                item.state = 1
            self.hotkey_menu.add(item)
        self.hotkey_menu.add(None)
        self.custom_hotkey_item = rumps.MenuItem("Eigene Kombination...", callback=self._custom_hotkey)
        self.hotkey_menu.add(self.custom_hotkey_item)

        self.whisper_menu = rumps.MenuItem("Whisper-Modell (Spracherkennung)")
        whisper_options = [
            ("tiny", "tiny — Sehr schnell, ungenauer (~75 MB)"),
            ("base", "base — Guter Kompromiss (~145 MB)"),
            ("small", "small — Gute Qualität (~465 MB)"),
            ("medium", "medium — Empfohlen, beste Qualität (~1.5 GB)"),
        ]
        for size, label in whisper_options:
            item = rumps.MenuItem(label, callback=self._make_whisper_callback(size))
            if size == self.config["whisper_model"]:
                item.state = 1
            self.whisper_menu.add(item)

        self.ollama_menu = rumps.MenuItem("Ollama-Modell (Textbereinigung)")
        ollama_options = [
            ("llama3.2", "llama3.2 — Klein & schnell (3B)"),
            ("llama3.1", "llama3.1 — Bessere Qualität (8B)"),
            ("mistral", "mistral — Gut für Deutsch/Europäisch (7B)"),
            ("gemma2", "gemma2 — Google, solide Qualität (9B)"),
            ("phi3", "phi3 — Microsoft, kompakt & gut (3.8B)"),
        ]
        for m, label in ollama_options:
            item = rumps.MenuItem(label, callback=self._make_ollama_callback(m))
            if m == self.config["ollama_model"]:
                item.state = 1
            self.ollama_menu.add(item)

        self.lang_menu = rumps.MenuItem("Sprache")
        lang_options = [
            (None, "Automatisch"),
            ("de", "Deutsch"),
            ("en", "English"),
            ("fr", "Français"),
            ("es", "Español"),
            ("it", "Italiano"),
        ]
        for code, label in lang_options:
            item = rumps.MenuItem(label, callback=self._make_lang_callback(code))
            if code == self.config["language"]:
                item.state = 1
            self.lang_menu.add(item)

        self.settings_menu.add(self.hotkey_menu)
        self.settings_menu.add(self.whisper_menu)
        self.settings_menu.add(self.ollama_menu)
        self.settings_menu.add(self.lang_menu)

        self.menu = [
            self.record_item,
            None,
            self.cleanup_item,
            self.settings_menu,
            None,
            self.status_item,
        ]

        self.status_item.title = "Status: Modell wird geladen..."
        threading.Thread(target=self._load_model, daemon=True).start()

        self._setup_event_tap()

    # --- Einstellungen-Callbacks ---

    def _make_hotkey_callback(self, hotkey_str):
        def callback(sender):
            self._apply_hotkey(hotkey_str, sender)
        return callback

    def _apply_hotkey(self, hotkey_str, selected_item=None):
        self.config["hotkey"] = hotkey_str
        self.hotkey = parse_hotkey(hotkey_str)
        self.hotkey_label = hotkey_display(hotkey_str)
        self.record_item.title = f"Aufnahme starten ({self.hotkey_label} halten)"
        for item in self.hotkey_menu.values():
            item.state = 0
        if selected_item:
            selected_item.state = 1
        save_config(self.config)
        rumps.notification("IQspeakr", "Hotkey geändert", f"Neuer Hotkey: {self.hotkey_label} halten")

    def _custom_hotkey(self, sender):
        response = rumps.Window(
            title="Eigene Tastenkombination",
            message="Gib deine Kombination ein, z.B.:\n\n"
                    "  ctrl+space\n"
                    "  alt+shift+r\n"
                    "  ctrl+shift+space\n"
                    "  f8\n\n"
                    "Verfügbare Tasten: ctrl, alt/option, shift, cmd,\n"
                    "space, enter, tab, f1-f12, oder ein Buchstabe (a-z)",
            default_text=self.config["hotkey"],
            ok="Übernehmen",
            cancel="Abbrechen",
        ).run()
        if response.clicked:
            hotkey_str = response.text.strip().lower()
            if hotkey_str:
                keys = parse_hotkey(hotkey_str)
                if keys:
                    self._apply_hotkey(hotkey_str)
                else:
                    rumps.notification("IQspeakr", "Fehler", f"Ungültige Kombination: {hotkey_str}")

    def _make_whisper_callback(self, size):
        def callback(sender):
            if size == self.config["whisper_model"]:
                return
            if not _whisper_model_cached(size):
                mb = {"tiny": 75, "base": 145, "small": 465, "medium": 1500}.get(size, 0)
                resp = rumps.alert(
                    title=f"Whisper-Modell '{size}' herunterladen?",
                    message=f"Das Modell ist noch nicht auf deinem Mac. Es werden ca. {mb} MB heruntergeladen.",
                    ok="Herunterladen",
                    cancel="Abbrechen",
                )
                if resp != 1:
                    return
            self.config["whisper_model"] = size
            for item in self.whisper_menu.values():
                item.state = 0
            sender.state = 1
            save_config(self.config)
            self.model = None
            self.status_item.title = f"Status: Lade Whisper '{size}'..."
            threading.Thread(target=self._load_model, daemon=True).start()
        return callback

    def _make_ollama_callback(self, model_name):
        def callback(sender):
            self.config["ollama_model"] = model_name
            for item in self.ollama_menu.values():
                item.state = 0
            sender.state = 1
            save_config(self.config)
            rumps.notification("IQspeakr", "Ollama-Modell geändert", model_name)
        return callback

    def _make_lang_callback(self, lang_code):
        def callback(sender):
            self.config["language"] = lang_code
            for item in self.lang_menu.values():
                item.state = 0
            sender.state = 1
            save_config(self.config)
            label = "Automatisch" if lang_code is None else lang_code
            rumps.notification("IQspeakr", "Sprache geändert", label)
        return callback

    def open_config(self, sender):
        subprocess.run(["open", CONFIG_PATH])

    # --- Modell laden ---

    def _load_model(self):
        self.model = whisper.load_model(self.config["whisper_model"])
        # Audio-Stream einmal kurz öffnen damit der erste Start sofort klappt
        try:
            s = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32')
            s.start(); s.stop(); s.close()
        except Exception:
            pass
        self.ollama_available = self._check_ollama()
        status = "Bereit"
        if not self.ollama_available:
            self.cleanup_enabled = False
            self.cleanup_item.title = "KI-Bereinigung: Aus (Ollama nicht gefunden)"
            status = "Bereit (ohne KI-Bereinigung)"
        self.status_item.title = f"Status: {status}"
        rumps.notification(
            title="IQspeakr",
            subtitle="Modell geladen",
            message=f"Whisper '{self.config['whisper_model']}' ist bereit.\n{self.hotkey_label} gedrückt halten = Aufnahme\n{self.hotkey_label} 2x tippen = Daueraufnahme",
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
                    "temperature": 0.1,  # wenig Kreativität → näher am Originaltext
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
                rumps.notification("IQspeakr", "", "Ollama läuft nicht. Starte Ollama zuerst.")
                return
        self.cleanup_enabled = not self.cleanup_enabled
        self.config["cleanup_enabled"] = self.cleanup_enabled
        state = "An" if self.cleanup_enabled else "Aus"
        sender.title = f"KI-Bereinigung: {state}"
        save_config(self.config)

    # --- Hotkey (NSEvent Global Monitor) ---

    def _setup_event_tap(self):
        mask = NSKeyDownMask | NSFlagsChangedMask

        def handler(event):
            keycode = event.keyCode()
            event_type = event.type()
            flags = event.modifierFlags()
            if event_type == 12:
                self._handle_modifier(keycode, flags)
            elif event_type == 10:
                self._handle_key(keycode, 10)

        monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, handler)
        if monitor is None:
            log.error("NSEvent Monitor konnte nicht erstellt werden!")
            rumps.notification("IQspeakr", "Fehler", "Bedienungshilfen-Berechtigung fehlt!")
            return

        self._event_monitor = monitor
        log.info("NSEvent Monitor aktiv — Hotkey-Erkennung läuft")

    def _handle_modifier(self, keycode, flags):
        hotkey_codes = self.hotkey
        if not hotkey_codes or len(hotkey_codes) != 1:
            return
        target = hotkey_codes[0]
        if keycode != target or target not in MODIFIER_KEYCODES:
            return

        now = _time.time()
        ctrl_pressed = bool(flags & (1 << 18))

        if ctrl_pressed:
            log.debug(f"Ctrl GEDRÜCKT (flags={flags:#x})")

            if self._continuous_mode and self.recording:
                log.info("Daueraufnahme gestoppt (Ctrl gedrückt)")
                self._continuous_mode = False
                self._stop_recording()
                return

            self._ctrl_press_time = now
            self._hold_mode = True
            if not self.recording:
                self._start_recording()

        else:
            log.debug(f"Ctrl LOSGELASSEN (flags={flags:#x})")
            hold_duration = now - self._ctrl_press_time if self._ctrl_press_time else 0

            if hold_duration < self._hold_threshold:
                if (now - self._last_tap_time) < self._double_tap_window and self._last_tap_time > 0:
                    log.info("Doppel-Tap erkannt → Daueraufnahme-Modus")
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
                    log.info(f"Ctrl losgelassen nach {hold_duration:.1f}s — transkribiere")
                    self._hold_mode = False
                    self._stop_recording()

    def _handle_key(self, keycode, event_type):
        hotkey_codes = self.hotkey
        if not hotkey_codes or len(hotkey_codes) != 1:
            return
        target = hotkey_codes[0]
        if keycode != target or target in MODIFIER_KEYCODES:
            return
        self.toggle_recording(None)

    def _cancel_recording(self):
        self.recording = False
        self.title = "🎤"
        self.record_item.title = f"Aufnahme starten ({self.hotkey_label} halten)"
        stream = self.stream
        self.stream = None
        self.audio_frames = []
        if stream:
            threading.Thread(target=self._close_stream_async, args=(stream,), daemon=True).start()
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
            rumps.notification("IQspeakr", "", "Modell wird noch geladen, bitte warten...")
            return

        log.info("Aufnahme gestartet")
        self.recording = True
        self.audio_frames = []
        self.title = "🔴"
        self.record_item.title = f"Aufnahme stoppen ({self.hotkey_label} loslassen)"

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
        self.title = "⏳"
        self.record_item.title = f"Aufnahme starten ({self.hotkey_label} halten)"
        log.info(f"Aufnahme gestoppt, {len(self.audio_frames)} Frames aufgenommen")

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
            self.title = "🎤"
            return

        self.audio_frames = frames
        self._transcribe()

    def _transcribe(self):
        audio_data = np.concatenate(self.audio_frames, axis=0).flatten()
        log.info(f"Transkribiere: {len(audio_data)} Samples, Peak: {np.max(np.abs(audio_data)):.4f}")

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                audio_int16 = (audio_data * 32767).astype(np.int16)
                wf.writeframes(audio_int16.tobytes())

            lang = self.config.get("language")
            log.info(f"Starte Whisper-Transkription (Sprache: {lang})...")
            try:
                result = self.model.transcribe(tmp.name, language=lang)
                raw_text = result["text"].strip()
                log.info(f"Whisper-Ergebnis: '{raw_text}' (Sprache: {result.get('language', '?')})")
            except Exception as e:
                log.error(f"Whisper-Fehler: {e}")
                rumps.notification("IQspeakr", "Fehler", str(e)[:100])
                return

            if raw_text:
                text = self._cleanup_text(raw_text)
                log.info(f"Bereinigter Text: '{text}'")
                subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
                log.info("Text in Zwischenablage kopiert")
                import time
                time.sleep(0.3)
                event_down = CGEventCreateKeyboardEvent(None, 9, True)
                CGEventSetFlags(event_down, kCGEventFlagMaskCommand)
                CGEventPost(kCGSessionEventTap, event_down)
                time.sleep(0.05)
                event_up = CGEventCreateKeyboardEvent(None, 9, False)
                CGEventSetFlags(event_up, kCGEventFlagMaskCommand)
                CGEventPost(kCGSessionEventTap, event_up)
                log.info("Cmd+V via CGEvent ausgeführt")
                log.info(f"Eingefügt: '{text}'")
            else:
                log.warning("Kein Text erkannt")
                rumps.notification("IQspeakr", "", "Kein Text erkannt.")
        except Exception as e:
            log.error(f"Transkriptions-Fehler: {e}", exc_info=True)
        finally:
            os.unlink(tmp.name)
            self.title = "🎤"


if __name__ == "__main__":
    IQspeakrApp().run()
