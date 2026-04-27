# IQspeakr — Windows

Windows-Portierung von IQspeakr. Lokale Sprache-zu-Text App mit System-Tray-Icon,
globalem Hotkey und optionaler KI-Textbereinigung via Ollama.

Der macOS-Code liegt unveraendert in `../IQspeakr/`. Dieses Verzeichnis ist
eine eigenstaendige Windows-Variante — kein geteilter Code, parallele Pflege.

## Was anders ist gegenueber macOS

| Aspekt | macOS | Windows |
|---|---|---|
| System-Tray | `rumps` | `pystray` + `Pillow` |
| Globaler Hotkey | NSEvent (AppKit) | `pynput.keyboard.Listener` |
| Paste | Quartz `CGEvent` → Cmd+V | `pynput.Controller` → Ctrl+V |
| Clipboard | `pbcopy` | `pyperclip` |
| Singleton-Lock | `fcntl.flock` | `msvcrt.locking` |
| Dialoge | `rumps.Window` | `tkinter.simpledialog` |
| Paket | `.app` + DMG | `.exe` via PyInstaller |
| Default-Hotkey | `ctrl` (hold) | `ctrl` (hold) |

Die Hold/Tap/Double-Tap-Logik ist 1:1 portiert — kurz halten = Aufnahme solange
gedrueckt, zweimal tippen = Daueraufnahme bis nochmal getippt.

## Voraussetzungen

- Windows 10 oder 11 (64 Bit)
- Python 3.10+ — <https://www.python.org/downloads/windows/>
- **ffmpeg.exe** im PATH (fuer Whisper). Zwei Wege:
  - `winget install Gyan.FFmpeg`
  - oder manuell: ffmpeg.exe nach `%USERPROFILE%\IQspeakr\bin\` legen — die App erweitert ihren PATH automatisch um dieses Verzeichnis
- Optional: **Ollama** fuer KI-Textbereinigung — <https://ollama.com/download/windows>. Muss auf `http://localhost:11434` laufen, ansonsten deaktiviert die App den Cleanup-Schritt automatisch.

## Entwicklung (ohne Build)

```powershell
# im Projekt-Ordner:
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Das System-Tray-Icon sollte erscheinen. Rechts/Links-Klick fuer das Menue.

## Build zu .exe

```powershell
.\build_exe.ps1
```

Ergebnis: `dist\IQspeakr.exe` (ca. 200–300 MB wegen Whisper + Torch).
Kann einzeln verschickt oder in den Autostart gelegt werden:

```powershell
# Oeffnet den Autostart-Ordner:
explorer shell:startup
# Dort eine Verknuepfung zu IQspeakr.exe reinlegen.
```

## Erster Start

1. `IQspeakr.exe` starten — das Tray-Icon erscheint.
2. Das Whisper-Modell wird beim ersten Start heruntergeladen (ca. 465 MB fuer `small`). Einmalig; danach lokal gecacht.
3. Standard-Hotkey: **Ctrl halten** zum Aufnehmen. Im Tray-Menue unter *Einstellungen → Tastenkombination* aenderbar.

## Dateien / Konfiguration

- `%APPDATA%\..\` wird nicht genutzt. Stattdessen:
  - `%USERPROFILE%\IQspeakr\config.json` — Einstellungen (Hotkey, Whisper-Modell, Sprache, Ollama-Modell).
  - `%USERPROFILE%\IQspeakr.log` — Debug-Log.
  - `%USERPROFILE%\.cache\whisper\*.pt` — heruntergeladene Whisper-Modelle.
  - `%TEMP%\iqspeakr.lock` — Singleton-Lock-Datei (wird automatisch verwaltet).

## Windows-spezifische Stolperfallen

- **Antivirus blockiert pynput-Keyboard-Hook**: Einige AV-Loesungen flaggen globale Keyboard-Listener. Ausnahme fuer `IQspeakr.exe` setzen, wenn der Hotkey nicht reagiert.
- **Ctrl allein** kollidiert mit vielen Programmen (Shortcuts). Wenn unbequem: Hotkey aendern zu `ctrl+space` (im Menue → Einstellungen → Tastenkombination → Eigene Kombination).
- **Mikrofon-Berechtigung**: Windows 10+ fragt beim ersten Aufnahme-Versuch nach. *Einstellungen → Datenschutz → Mikrofon* muss „Apps Zugriff gewaehren" aktiv haben.
- **Toast-Benachrichtigungen**: Nutzen das Win10+ Notification-System. Wenn „Fokus-Assistent" an ist, werden sie stumm gestellt.

## Bekannte Unterschiede zum macOS-Original

- Keine `Cmd`-Taste — die Option „Cmd halten" heisst auf Windows `Win halten` (Windows-Key / Super).
- Kein Dock-Icon-Verhalten — nur System-Tray.
- Das Tray-Icon ist ein einfaches farbiges Rund (grau = bereit, rot = Aufnahme, orange = verarbeitet), nicht das macOS-Menubar-Icon-Set.

## Debugging

- Log-Datei: `%USERPROFILE%\IQspeakr.log` (DEBUG-Level)
- Mehrere Instanzen testen: `IQspeakr.exe` zweimal starten — die zweite muss sich sofort beenden (Log: „IQspeakr laeuft bereits — zweite Instanz beendet sich.").
- Hotkey matcht nicht: im Log nach „NSEvent"/„pynput"/„toggle_recording" suchen.
