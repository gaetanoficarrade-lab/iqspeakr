# Handoff: `macos_v3/` — Polish-Release auf Basis von `macos_v2/` (2026-04-27)

**Status: Eingefroren. An v3 darf nichts mehr verändert werden** — der User hat die Version explizit abgenommen und gesperrt. Künftige Änderungen gehen in einen separaten Branch oder eine v4-Kopie.

`macos_v3/` ist eine 1:1-Kopie von `macos_v2/` (Stand `v2.1-tahoe-tcc-fixed`, Commit `ab9d8d9`) mit drei gezielten UX-Korrekturen. Die gesamte Architektur (C-Mach-O-Launcher, Python-im-Bundle, Tray-Subprocess via NSStatusItem, pynput-Monkey-Patch, Audio-Stream-Guard, LSUIElement-Setup) ist **unverändert** übernommen — siehe `HANDOFF_MACOS_V2.md` für die Tiefenerklärung.

## Was v3 anders macht (drei Änderungen, sonst nichts)

### 1. Ollama-Abfrage entfernt
Der Installer fragte am Ende „Möchtest du Ollama installieren für die optionale KI-Textbereinigung?". Ollama war/ist nicht mehr Teil des Setups — diese Frage darf nicht mehr kommen.

- `installer/gui/installer`: kompletter Block „6. Ollama" entfernt (`WANT_OLLAMA`-osascript-Dialog, Download, Modell-Pull). Auch die `OLLAMA_URL`-Konstante raus.
- App-internes Ollama-Tooling im Tray-Menü (`_build_ollama_submenu`, `_check_ollama`, `OLLAMA_URL = "http://localhost:11434/api/generate"`) wurde **bewusst nicht** angefasst — wer Ollama selbst installiert, kann es weiter nutzen, aber der Installer drängt es niemandem mehr auf.

### 2. Berechtigungs-Wizard erklärt den exakten Pfad
Der Wizard beim ersten Start (`_show_accessibility_hint` in `app.py`) sagte vorher nur „Tab öffnet sich automatisch", was nicht reichte, wenn der Tab nicht erschien oder der User die Settings später erneut suchen musste.

Beide Punkte (1. Eingabeüberwachung, 2. Bedienungshilfen) zeigen jetzt explizit den Pfad:

> Apple-Menü → Systemeinstellungen → Datenschutz & Sicherheit → Eingabeüberwachung
>
> Apple-Menü → Systemeinstellungen → Datenschutz & Sicherheit → Bedienungshilfen

Die übrigen Schritte (`+`-Knopf → Programme → IQspeakr → Schalter EIN, TouchID-Bestätigung, automatisches Polling) bleiben gleich.

### 3. Startup-Splash — sichtbar **bevor** schwere Imports laufen
Auf dem ersten Start dachte der User, die App startet nicht: zwischen Doppelklick und Tray-Icon vergingen 1–3 Sekunden ohne sichtbares Feedback (whisper/torch/sounddevice/pynput laden).

**Lösung in `app.py`:** Direkt nach dem Singleton-Check (vor `import numpy/sounddevice/whisper/torch/pynput`) wird ein minimales Bootstrap ausgeführt:

```python
from PySide6.QtCore import Qt as _Qt
from PySide6.QtWidgets import QApplication as _QApplication, ...

class _StartupSplash(_QWidget):
    # 380x150 frameless, zentriert, indeterminate progress bar
    ...

_qapp = _QApplication(sys.argv)
_qapp.setQuitOnLastWindowClosed(False)
_splash = _StartupSplash()
_splash.show()
_qapp.processEvents()
```

Erst danach kommen die schweren Imports. `main()` übernimmt `_qapp` und `_splash` aus den Modul-Globals statt eigene zu erzeugen.

Splash schließt sich automatisch:
- bei Status `"Bereit"` (in `IQspeakrApp._on_status` — Whisper-Modell ist geladen + Audio-Stream offen)
- spätestens nach 30 s als Sicherheitsnetz (`QTimer.singleShot` in `main()`)

**Wichtig bei Refactors:** Der Splash-Bootstrap MUSS am Modul-Anfang stehen, nicht in `main()`. PySide6 ist dabei der einzige Import, der hochgezogen werden darf — alles andere (whisper, torch, sounddevice, pynput, pynput-Monkey-Patch) bleibt unten, sonst friert die UI 1–3 s ein, bevor das Fenster erscheint.

## Datei-Änderungen kompakt

| Datei | Änderung |
|---|---|
| `app.py` | Splash-Bootstrap am Modul-Anfang; `IQspeakrApp.__init__(qapp, splash=None)` mit `self._splash`; `_on_status` schließt Splash bei "Bereit"; `main()` nutzt `_qapp`/`_splash`-Globals; Wizard-Text mit explizitem Pfad |
| `installer/gui/installer` | Ollama-Block + `OLLAMA_URL`-Konstante komplett raus |
| `build_dmg.sh` | v2 → v3 (Echo, Volume-Name, DMG-Output, CFBundleVersion 3.0) |
| `IQspeakr-v3-Installer.dmg` | Neu gebaut (~80 KB; nicht im Repo, GitGB ignoriert `*.dmg`) |

Alle anderen Dateien (`launcher.c`, `tray_proc.py`, `config.json`, `requirements.txt`, `README.md`, `IQspeakr.icns`) sind byte-identisch zu `macos_v2/`.

## Build & Verteilung

```bash
cd macos_v3
./build_dmg.sh
# -> IQspeakr-v3-Installer.dmg (~80 KB)
```

Die DMG geht in einen GitHub-Release (manuell hochladen), nicht ins Repo (`*.dmg` in `.gitignore`).

## Saubere Reinstallation

User-spezifische Pfade nach Deinstallation:

| Pfad | Was | Bei Reinstall? |
|---|---|---|
| `~/IQspeakr/` | Config | löschen für „frischen" Start |
| `~/IQspeakr.log`, `~/IQspeakr.crash.log` | Logs | löschen |
| `~/.iqspeakr/` | Bundle-Python + venv | löschen — Installer baut neu |
| `~/.cache/whisper/` (~3,5 GB) | Whisper-Modelle | **behalten**, sonst lädt Installer Modelle erneut runter |
| `/tmp/iqspeakr.pid` | Singleton-Lock | egal, Reboot räumt das |

Stand 2026-04-27 vom User getestet: Deinstallation + saubere Neuinstallation aus v3-DMG funktioniert, Wizard zeigt korrekten Pfad, kein Ollama-Dialog mehr, Splash erscheint früh genug.
