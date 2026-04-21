# Handoff: Mac → Windows

Übergabedokument von der Mac-Session an die Windows-Session.
Claude Code auf Windows: **als erstes diese Datei lesen**, dann weitermachen.

---

## Kontext des Projekts

IQspeakr = lokale Sprache-zu-Text-App mit globalem Hotkey. Zwei parallele
Codebases im gleichen Repo:

- `macos/` — bestehende macOS-Version (rumps + PyObjC), **unverändert lassen**
- `windows/` — frisch portierte Windows-Version (pystray + pynput), **aktiver Arbeitsbereich**

Repo: <https://github.com/gaetanoficarrade-lab/iqspeakr> (privat, Account `gaetanoficarrade-lab`).

---

## Wo wir auf Mac stehen geblieben sind

1. Mac-Version läuft stabil, DMG funktioniert.
2. Windows-Port ist geschrieben, aber **noch nie auf Windows getestet**.
3. Repo ist auf GitHub gepusht (`main`).
4. User hat auf dem Windows-PC gerade:
   - Python 3.12, ffmpeg, git, gh CLI via winget installiert
   - Repo gecloned (vermutlich nach `C:\Projekte\iqspeakr` oder ähnlich — den User fragen)
   - `.venv` erstellt + aktiviert
   - `pip install -r requirements.txt` läuft/ist durch
5. Nächster Schritt: `python app.py` ausführen und schauen ob's startet.

---

## Was auf Windows als Nächstes passieren soll

### Schritt 1: pip install abschließen
User sollte in `iqspeakr\windows\`:
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
Bei ERROR im Output → Fehler kopieren, debuggen. Bei „Successfully installed ..." → weiter.

### Schritt 2: App starten
```powershell
python app.py
```
Erwartet: Tray-Icon im Task-Bereich unten rechts (evtl. im versteckten-Symbole-Menü unter dem `^`-Pfeil). Farbiger Kreis — grau = bereit, rot = Aufnahme, orange = verarbeitet.

**Erster Start lädt Whisper-Modell** (~465 MB für `small`). Kann ein paar Minuten dauern. Danach poppt ggf. eine Windows-Benachrichtigung „Modell geladen".

### Schritt 3: Smoke-Test
- Ctrl halten, einen Satz sprechen, loslassen → Text sollte in ein offenes Textfeld (z.B. Notepad) per Ctrl+V eingefügt werden.
- Zweite Instanz testen: zweites PowerShell öffnen, nochmal `python app.py` → muss sich sofort beenden, **kein** zweites Tray-Icon.

### Schritt 4: Falls alles läuft → `.exe` bauen
```powershell
.\build_exe.ps1
```
Ergebnis: `dist\IQspeakr.exe` (200-300 MB, single-file). Die kann der User per Drive/Dropbox an andere weitergeben.

---

## Architektur-Unterschiede macOS ↔ Windows

| Aspekt | macOS (`macos/`) | Windows (`windows/`) |
|---|---|---|
| Tray-Icon | `rumps.App` | `pystray.Icon` + PIL-Kreis |
| Global Hotkey | AppKit NSEvent Monitor | `pynput.keyboard.Listener` |
| Paste | Quartz CGEvent → Cmd+V | `pynput.Controller` → Ctrl+V |
| Clipboard | `subprocess.run(["pbcopy"])` | `pyperclip.copy()` |
| Singleton-Lock | `fcntl.flock(LOCK_EX\|LOCK_NB)` auf `/tmp/iqspeakr.pid` | `msvcrt.locking(LK_NBLCK)` auf `%TEMP%\iqspeakr.lock` |
| Dialoge | `rumps.Window` / `rumps.alert` | `tkinter.simpledialog` / `messagebox` |
| Config-Datei öffnen | `subprocess.run(["open", path])` | `os.startfile(path)` |
| Paket-Format | `.app` + DMG via `build_dmg.sh` | `.exe` via `build_exe.ps1` (PyInstaller) |

Die **Hold/Tap/Double-Tap-State-Machine** (Ctrl halten = Aufnahme solange, 2× tippen = Daueraufnahme) ist 1:1 portiert — Timings 0.3s Hold-Threshold, 0.4s Double-Tap-Fenster.

---

## Wichtige Regeln (nicht brechen!)

1. **Singleton-Lock-FD darf NIE geschlossen werden** während die App läuft. `_lock_fd` ist Modul-Global. Wenn jemand das in ein `with`-Statement packt oder explizit schließt, gibt der Kernel den Lock frei → mehrere Instanzen werden wieder möglich. Das war der ursprüngliche Bug, der auf Mac gefixt wurde und auf Windows genauso gilt.

2. **`sd.InputStream.stop()` und `.close()` im Background-Thread** (nicht im Main-Thread). Auf macOS war das zwingend wegen CoreAudio-Mutex-Deadlock. Auf Windows ist es weniger kritisch, aber das Muster bleibt (Code nutzt dafür `_stream_lock` + `_close_stream_async`).

3. **macOS-Code (`macos/app.py`) nicht anfassen**. Der läuft stabil, jeder Windows-Fix gehört in `windows/app.py`.

---

## Wichtige Pfade & Kommandos auf Windows

```powershell
# Log live mitlesen
Get-Content $env:USERPROFILE\IQspeakr.log -Wait -Tail 50

# Config anschauen
notepad $env:USERPROFILE\IQspeakr\config.json

# Stale-Lock manuell löschen (falls je nötig)
Remove-Item $env:TEMP\iqspeakr.lock

# Laufende Python-Instanzen finden
Get-Process python -ErrorAction SilentlyContinue | Format-Table Id, ProcessName, Path

# Kill aller iqspeakr-Instanzen
Get-Process python | Where-Object { $_.Path -like '*iqspeakr*' } | Stop-Process
```

**Config-Pfade** (alle unter User-Home):
- `%USERPROFILE%\IQspeakr\config.json` — Einstellungen
- `%USERPROFILE%\IQspeakr.log` — DEBUG-Log
- `%USERPROFILE%\.cache\whisper\*.pt` — Whisper-Modelle
- `%TEMP%\iqspeakr.lock` — Single-Instance-Lock

---

## Mögliche Stolperfallen (erfahrungsbasiert)

- **AV-Tool blockt pynput Keyboard-Listener**: Windows Defender oder 3rd-Party-AV sperrt manchmal Low-Level-Keyboard-Hooks. Symptom: App läuft, aber Ctrl-Drücken löst nichts aus. Fix: Ausnahme für `python.exe` (bzw. später `IQspeakr.exe`) in AV-Einstellungen.

- **Ctrl allein als Hotkey kollidiert** mit sehr vielen Apps (Shortcuts). Falls es im Alltag nervig ist → im Tray-Menü Einstellungen → Tastenkombination → Eigene Kombination → `ctrl+space`. Falls das besser funktioniert, ggf. als neuen Default-Wert in `config.json` setzen.

- **Mikrofon-Berechtigung**: Windows 11 fragt beim ersten Aufnahme-Versuch. Falls vergessen → Einstellungen → Datenschutz → Mikrofon → Apps Zugriff gewähren.

- **Tkinter-Dialog aus pystray-Callback**: pystray-Callbacks laufen in einem Worker-Thread, nicht im Main. Die Windows-Version erstellt pro Dialog einen frischen tkinter-Root + zerstört ihn wieder. Falls Dialoge hängen → in `_custom_hotkey`-Funktion nachgucken.

- **`os.startfile(CONFIG_PATH)`**: Öffnet `.json` mit dem Default-Programm. Wenn keine Zuordnung besteht, fragt Windows. Alternativ `Start-Process notepad $path`.

- **PyInstaller + Whisper**: Whisper hat eigene Asset-Dateien (Tokenizer). Das Build-Script nutzt `--collect-all whisper`. Falls im Build `.exe` läuft aber „No module named whisper.model" kommt → `--collect-all whisper` prüfen.

---

## Memory-Transfer (optional)

Der Mac hat unter `~/.claude/projects/-Users-gaetanoficarra/memory/` Memory-Dateien mit Projekt-Kontext. Die wichtigsten:

- `project_iqwhisperer.md` — Vorgänger-Projekt „spreakr"
- `project_spreakr_audio_deadlock.md` — **CoreAudio-Deadlock-Regel** (nur macOS, aber Hintergrund)
- `project_iqspeakr_github_idea.md` — Überlegungen zum GitHub-Release (jetzt teils erledigt)

Der User kann sie bei Bedarf manuell rüberkopieren (iCloud Drive, USB, was auch immer), zum Pfad `%USERPROFILE%\.claude\projects\...\memory\` — dann sieht Windows-Claude-Code sie automatisch. Nicht zwingend nötig, dieses HANDOFF.md reicht für den Moment.

---

## Erste Nachricht an Claude Code auf Windows

Etwa so:

> Ich habe das Repo gecloned und `pip install` läuft. Bitte lies HANDOFF.md und dann sag mir was ich als Nächstes tun soll.

Claude Code liest dann dieses Dokument und führt den User durch Schritt 2-4.
