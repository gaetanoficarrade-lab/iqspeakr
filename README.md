# IQspeakr

Lokale Sprache-zu-Text App mit globalem Hotkey, Whisper-Transkription und
optionaler KI-Textbereinigung via Ollama. Keine Cloud-APIs — alles laeuft
auf deinem Rechner.

Zwei parallele Ports:

- **[`macos/`](./macos)** — macOS-Original, `rumps` + PyObjC, `.app` + DMG-Installer
- **[`windows/`](./windows)** — Windows-Port, `pystray` + `pynput`, `.exe` via PyInstaller

Beide Versionen haben dieselbe UX: Hotkey halten = aufnehmen, 2x tippen =
Daueraufnahme, loslassen = transkribieren + per Ctrl/Cmd+V einfuegen.

## Fuer Endnutzer

Fertige Installer gibt's unter [Releases](../../releases). Einfach die
zum System passende Datei herunterladen:

- `IQspeakr-Installer.dmg` → macOS
- `IQspeakr.exe` → Windows

Kein GitHub-Account, kein Python, keine Kommandozeile noetig.

## Fuer Entwickler

- macOS-Build: `cd macos && ./build_dmg.sh`
- Windows-Build: `cd windows && .\build_exe.ps1`

Details jeweils in den Unterordner-READMEs.

## Lizenz

Alle Rechte vorbehalten. Keine Weiterverbreitung, Modifikation oder
kommerzielle Nutzung ohne ausdrueckliche Genehmigung.
