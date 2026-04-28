# IQspeakr macOS v4

Mac-Variante mit vollem Funktionsumfang von `../windows_v2/`: vier-View-
Hauptfenster (Home/Dashboard/Style/Settings) zusaetzlich zum Tray + Hotkey,
plus alle macOS-26-Stolperstein-Fixes aus v2/v3 (NSStatusItem-Subprocess,
pynput-Monkey-Patch, C-Mach-O-Launcher fuer TCC-Identity, Splash-Bootstrap).

`../macos/` bleibt als stabiles Backup. v2 ist der Tahoe-TCC-Fix, v3 die
Polish-Variante (eingefroren). v4 baut darauf auf — Aenderungen ab jetzt
nur hier in `macos_v4/`.

## Was v4 anders macht

- Vollwertiges Hauptfenster mit vier Views (Home, Dashboard, Style, Settings) — erreichbar per Tray-Menu-Eintrag "Hauptfenster oeffnen". Beim Schliessen versteckt sich das Fenster, App lebt im Tray weiter.
- Aufnahme-History (letzte 10 Transkripte) und Dashboard mit Streak / WPM / Aktivitaets-Heatmap, persistiert in `~/IQspeakr/history.json` und `~/IQspeakr/stats.db` (SQLite).
- Style-Presets fuer KI-Textbereinigung: Foermlich / Locker / Sehr locker / Individuell (mit Checkbox-Builder). Setzt aktive Ollama-Installation voraus; auf macOS oeffnet der Setup-Button die Ollama-Download-Seite (kein silent install wie auf Windows).

## Was anders ist gegenüber `../macos/` (Legacy)

| Aspekt | macos/ (alt) | macos_v2/ (neu) |
|---|---|---|
| Tray/Menu | `rumps` | `PySide6 QSystemTrayIcon` |
| STT | `openai-whisper` (CPU) | `openai-whisper` + MPS (Apple Silicon GPU) |
| Hotkey | `AppKit.NSEvent` | `pynput.keyboard.Listener` |
| Audio | Open/close pro Aufnahme | Persistenter Stream, Flag-gated |
| Visual Feedback | — | Pill-Waveform-Overlay unten |
| Custom-Hotkey | `rumps.Window` Freitext | Live-Recorder-Dialog |
| Multi-Modifier | Nein (nur Single-Modifier-Hold) | Ja (z.B. Ctrl+Shift halten) |

Paste (Cmd+V), Singleton-Lock (`fcntl.flock` auf `/tmp/iqspeakr.pid`) und
CoreAudio-Deadlock-Schutz (Stream-Cleanup im Background-Thread) bleiben.

## Voraussetzungen

- Apple Silicon Mac (Intel geht auch, dann CPU statt MPS)
- macOS 12+
- Python 3.11+: `brew install python@3.12` oder python.org
- ffmpeg: `brew install ffmpeg`
- Optional: Ollama für KI-Textbereinigung — `brew install ollama && ollama serve`

## Entwicklung

```bash
cd macos_v2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Beim ersten Start fragt macOS nach:
- **Bedienungshilfen-Permission** (für pynput-Keyboard-Hook)
- **Input Monitoring** (neuere macOS-Versionen, zusätzlich)
- **Mikrofon-Zugriff** (beim ersten Aufnahme-Versuch)

Alle drei in Systemeinstellungen → Datenschutz & Sicherheit freigeben, dann
App einmal neu starten.

## Build zu .app + DMG

```bash
./build_dmg.sh
```

Ergebnis:
- `dist/IQspeakr.app` — einzeln startbar
- `IQspeakr-v2.dmg` — verteilbar (ziehen nach Programme)

Das Info.plist bekommt `LSUIElement=true`, d.h. die App hat kein Dock-Icon,
nur das Menüleisten-Icon oben rechts.

## Defaults

- Hotkey: `ctrl+shift` halten (cross-platform konsistent mit Windows)
- Whisper-Modell: `base`
- Sprache: `de`
- Paste-Delay: 50 ms

Im Tray-Menü änderbar. Konfig liegt in `~/IQspeakr/config.json`.

## Log + Debug

- Log: `~/IQspeakr.log` (DEBUG-Level)
- Crash-Log (falls aktiviert): `~/IQspeakr.crash.log`
- Singleton-Lock: `/tmp/iqspeakr.pid`
- Whisper-Cache: `~/.cache/whisper/*.pt`

Zweite Instanz testen:
```bash
python3 app.py   # Terminal 1
python3 app.py   # Terminal 2 — sollte sofort exiten
```
