# Handoff: Windows → Mac (Rewrite `macos_v2/`)

Übergabedokument von der Windows-Session an die macOS-Session.
**Claude Code auf Mac: als erstes diese Datei komplett lesen**, dann mit dem User
die unten markierten Design-Fragen kurz klären, dann loslegen.

---

## Kontext

IQspeakr = lokale Sprache-zu-Text-App mit globalem Hotkey. Im Repo liegen aktuell
zwei parallele Codebases:

- `macos/installer/app.py` — bestehende Mac-Version. **Unverändert lassen.** Läuft
  stabil, baut ein DMG (`macos/build_dmg.sh`). Stack: `rumps` + `openai-whisper` +
  `AppKit`/`Quartz` NSEvent-Monitor, Stream open/close pro Aufnahme.
- `windows/app.py` — Windows-Version. Seit dem letzten Mac-Handoff (Apr 21)
  **stark weiterentwickelt**: kompletter Refactor auf `PySide6 QSystemTrayIcon`
  + `faster-whisper` + `pynput` + persistenten Audio-Stream, plus massig kleiner
  Features und Stabilitäts-Fixes.

Repo: <https://github.com/gaetanoficarrade-lab/iqspeakr> (privat).
Aktueller `main` nach Push: Commit enthält die neue Windows-Version inkl.
Inno-Installer, Default-Hotkey Strg+Umschalt, Whisper-base-Default, HotkeyRecorder,
deutsche Windows-UI-Labels.

---

## Auftrag

Eine **Kopie der bestehenden Mac-Version als `macos_v2/` anlegen** (neben
`macos/`, nicht darüber!), und in `macos_v2/` einen **kompletten Rewrite** auf
den Windows-Stack durchführen, damit Mac und Windows Feature- und Strukturparität
haben. `macos/` bleibt als Backup liegen.

Der User hat explizit den großen Rewrite gewählt (nicht den kleinen Port),
**unter der Voraussetzung, dass am Ende alles auf dem Mac läuft**. Also: Build
und Smoke-Test sind Pflicht-Abschluss.

---

## Design-Entscheidungen — mit User klären, bevor du loslegst

Die haben konkrete Konsequenzen; bitte nicht einseitig entscheiden.

### 1) Whisper-Library: `faster-whisper` oder `openai-whisper` + MPS?
- **`faster-whisper`** (was Windows nutzt): CPU-only auf Mac, weil `ctranslate2`
  keine Metal-/MPS-Unterstützung hat. Stabil, aber auf Apple Silicon **langsamer**
  als die GPU-Variante.
- **`openai-whisper`** mit `torch.backends.mps`: nutzt die Apple-Silicon-GPU
  (Metal). Auf M1/M2/M3 typisch 3–5× schneller als CPU. Hat auf Windows den
  Torch-/MKL-Crash im PyInstaller-Bundle ausgelöst, der der eigentliche Grund
  für den Wechsel zu `faster-whisper` war — das Problem tritt auf Mac so **nicht**
  auf (kein MKL, kein PyInstaller-`--onedir` mit dem gleichen Binding-Drama).

Empfehlung: **`openai-whisper` + MPS** auf Mac, weil das performance-entscheidend
ist und die Crash-Vektoren Windows-spezifisch waren. Der Rest der Architektur
(PySide6, persistenter Stream, State-Machine) ist davon unabhängig und wird
trotzdem portiert.

### 2) Default-Hotkey: `cmd+shift` oder `ctrl+shift`?
- Mac-Idiom ist `cmd`. Aber der User hat auf Windows bewusst **Strg+Umschalt**
  als neuen Default gewählt.
- `cmd+shift` auf Mac wäre das mac-idiomatische Äquivalent.
- `ctrl+shift` auf Mac wäre cross-platform konsistent, aber auf Mac-Tastaturen
  unüblich als Hotkey (linke Ctrl-Taste liegt weit weg).

Empfehlung: **`cmd+shift`** als Mac-Default.

### 3) UI-Labels für Modifier
Windows nutzt deutsche Windows-Konvention: `Strg`, `Umschalt`, `Alt`, `Win`,
`Leertaste`, `Eingabe`. Auf Mac wäre das falsch.

Empfehlung: **Unicode-Symbole wie macOS-Standard**: `⌘`, `⇧`, `⌥`, `⌃`. So
macht's die bestehende Mac-Version auch schon (`HOTKEY_DISPLAY` in
`macos/installer/app.py:117`).

### 4) Hotkey-Listener: `pynput` oder `NSEvent`?
- `pynput` auf Mac nutzt intern Quartz + NSEvent. Funktioniert, braucht
  **Accessibility Permission** (System Settings → Privacy & Security →
  Accessibility — Terminal bei Dev-Run, `.app` bei Build).
- `NSEvent.addGlobalMonitorForEventsMatchingMask_handler_` wie in der aktuellen
  Mac-Version: braucht dieselbe Permission, ist aber nativer.

Empfehlung: **`pynput`** für Stack-Parität mit Windows. Der User kennt das
Accessibility-Prompt bereits, weil die alte Mac-Version es auch brauchte.

### 5) Pill-Overlay: beibehalten?
Windows hat ein Live-Waveform-Overlay (7 Balken, unten-mittig). Mac hat's nicht.
Kleine Mac-Anpassung nötig: `MARGIN_BOTTOM` soll Dock berücksichtigen, nicht
Taskleiste. Und `LSUIElement=YES` im `Info.plist` sorgt dafür, dass die App
kein Dock-Icon zeigt (äquivalent zu `setActivationPolicy_(1)` in der Alt-Version).

Empfehlung: **Ja, übernehmen.** Nettes visuelles Feedback, Cross-platform-Code.

---

## Was auf Windows jüngst gebaut wurde (alles in `windows/app.py`, Commit d2e0f53)

Damit du weißt, was du portieren sollst. Zeilenzahlen sind Stand d2e0f53.

- **Persistenter Audio-Stream**: wird einmal in `_load_model` geöffnet
  (`self._persistent_stream`), lebt bis `_quit`. Aufnahme wird per `self.recording`-
  Flag im Callback gated. Grund: PortAudio-Race bei rapidem Stream-Open/Close
  crasht unter Windows. Auf Mac wahrscheinlich weniger kritisch, aber die
  Architektur ist trotzdem sauberer.
- **`PillOverlay` (QWidget)**: 180×36 unten-mittig, 7-Balken-Waveform, fading
  opacity. Thread-safe über atomische Attribut-Writes aus Audio-Callback + QTimer-
  Poll im Main-Thread. Siehe `windows/app.py:119-210`.
- **`HotkeyRecorderDialog` (QDialog)**: User drückt Kombi, Dialog zeigt sie live,
  speichert auf „Übernehmen". Ersetzt die alte Freitext-`QInputDialog`-Variante,
  die bei Sonderzeichen/Tippfehlern nicht robust war. Siehe `windows/app.py:363-477`.
- **Multi-Modifier-Hold-State-Machine**: Hold/Tap/Double-Tap funktioniert jetzt
  auch für Kombis aus mehreren Modifiern (z.B. Strg+Shift). Trigger via
  `_hotkey_is_all_modifiers`. Siehe `windows/app.py:305-317` (Helper),
  `windows/app.py:894-956` (Press/Release-Handler).
- **Faster-Whisper-Optionen**: `beam_size=1`, `vad_filter=True`, 
  `condition_on_previous_text=False`. Spart spürbar Zeit bei kurzen Samples.
  Siehe `windows/app.py:1030-1042`.
- **Defaults**: `hotkey=ctrl+shift`, `whisper_model=base`, `language=de`. Siehe
  `windows/app.py:329-337` (`DEFAULT_CONFIG`).
- **Paste-Delay**: von 300 ms auf 50 ms runter. Siehe `windows/app.py:1086-1094`.
- **`QActionGroup` + Check-Items** im Tray-Menü für Hotkey/Whisper/Ollama/Sprache.
- **Singleton**: `msvcrt.locking` (Windows). Auf Mac `fcntl.flock` wie in der
  bestehenden Mac-Version (`macos/installer/app.py:47-66`) — übernehmbar 1:1.

Nicht direkt übertragen werden muss:
- `faulthandler` auf crash-log-File (auf Mac weniger kritisch, kann aber mit
  übernommen werden, schadet nicht).
- `os.environ["OMP_NUM_THREADS"]=1` etc. (das war MKL-spezifisch für Windows).

---

## Architektur-Mapping Windows → Mac

| Konzept | Windows (aktuell) | Mac alt (`macos/installer/app.py`) | Mac v2 (Ziel) |
|---|---|---|---|
| Tray/Menu-Framework | `PySide6.QSystemTrayIcon` | `rumps` | **`PySide6.QSystemTrayIcon`** |
| STT-Engine | `faster-whisper` (CPU int8) | `openai-whisper` | **`openai-whisper` + MPS** (s. Frage 1) |
| Hotkey-Listener | `pynput.keyboard.Listener` | `NSEvent.addGlobalMonitor…` | **`pynput`** (s. Frage 4) |
| Paste-Simulation | `pynput.keyboard.Controller` → Ctrl+V | `CGEventCreateKeyboardEvent` + Cmd-Flag → Cmd+V | **`pynput` → Cmd+V** |
| Audio-Stream | persistent, `sounddevice.InputStream` | open/close pro Aufnahme | **persistent** |
| Singleton | `msvcrt.locking` | `fcntl.flock` | **`fcntl.flock`** |
| Pfad `~/IQspeakr/` | `Path.home() / "IQspeakr"` | `os.path.expanduser("~/IQspeakr")` | `Path.home() / "IQspeakr"` |
| Dock-Icon unterdrücken | (braucht's nicht) | `NSApplication.sharedApplication().setActivationPolicy_(1)` | **`Info.plist` LSUIElement=YES** beim Build + setActivationPolicy zur Laufzeit als Fallback |
| Custom-Hotkey-UI | `HotkeyRecorderDialog` (PySide6) | `rumps.Window` Freitext | **`HotkeyRecorderDialog`** (1:1 übernehmen) |
| Pill-Overlay | `PillOverlay` QWidget | — | **übernehmen**, `MARGIN_BOTTOM` an Dock anpassen |
| Pre-torch-ENV | `OMP_NUM_THREADS=1` etc. | — | nicht nötig |
| faulthandler-Log | `~/IQspeakr.crash.log` | — | optional übernehmen |

---

## Plattform-Gotchas (Mac-spezifisch)

1. **Accessibility Permission**: `pynput` funktioniert erst, wenn die ausführende
   App (Terminal / IQspeakr.app) in *System Settings → Privacy & Security →
   Accessibility* freigegeben ist. Beim ersten Start-Fehler: prompt mit Hinweis
   anzeigen. Alte Mac-Version hatte das als `rumps.notification("…Bedienungshilfen-
   Berechtigung fehlt!")` — ähnlichen Fallback einbauen.
2. **Input Monitoring Permission**: zusätzlich zu Accessibility auf neueren macOS.
3. **PyInstaller auf Mac**: baut `.app`-Bundle, nicht `--onedir`-Ordner. Flags
   etwa: `--windowed --onedir --osx-bundle-identifier com.iqspeakr.app`. DMG
   danach via bestehendem `build_dmg.sh` (anpassen an neues Verzeichnis).
4. **Code-Signing + Notarization**: Für Distribution zwingend, sonst Gatekeeper
   blockt. `codesign --deep --force --sign "Developer ID Application: …"` +
   `xcrun notarytool submit`. Ohne Developer-Account: User muss beim ersten
   Start rechts-klicken → „Öffnen" → Warnung bestätigen. Mit User klären, ob
   Signing relevant ist (war's auf Windows auch nicht, SmartScreen-Warnung
   wurde hingenommen).
5. **Apple Silicon vs Intel**: Falls `openai-whisper` + MPS genutzt wird, nur
   auf Apple Silicon schnell. Intel-Macs bleiben CPU. Check via
   `torch.backends.mps.is_available()`.
6. **NSApplicationActivationPolicy**: wenn das Qt-Fenster trotzdem einen
   Dock-Eintrag erzeugt, zusätzlich zur Laufzeit setzen:
   ```python
   from AppKit import NSApplication
   NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory
   ```

---

## Schrittweiser Plan

### 1. Vorbereitung am Mac
```bash
cd ~/Projekte/iqspeakr   # (oder wo auch immer dein Clone liegt)
git pull origin main
cp -r macos macos_v2
cd macos_v2
```
Dabei entsteht `macos_v2/{IQspeakr.icns, build_dmg.sh, installer/}`. Die
Struktur drin behalten oder auf flach (wie `windows/`) umstellen — nach
User-Wunsch. Empfehlung: **flach** (weniger Nesting), also Inhalt von
`macos_v2/installer/` nach `macos_v2/` rauf holen.

### 2. venv und Dependencies
```bash
cd macos_v2
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```
`requirements.txt` anlegen (neu, nicht aus alter macos/ übernehmen):
```
PySide6
openai-whisper          # ODER faster-whisper — siehe Design-Frage 1
numpy
sounddevice
pynput
pyperclip
torch                   # falls openai-whisper + MPS
```
`pip install -r requirements.txt`.

### 3. app.py schreiben
Basis: `windows/app.py` 1:1 übernehmen, dann folgendes anpassen:
- Imports: `import msvcrt` → `import fcntl`. Lock-Pfad auf `/tmp/iqspeakr.pid`.
  Logik wie in `macos/installer/app.py:47-66`.
- Entfernen: `OMP_NUM_THREADS` etc. setzen am Dateianfang — auf Mac nicht nötig.
- `_BUNDLE_BIN`-PATH-Frickelei: Mac-Version hat `~/.iqspeakr/bin` +
  `/opt/homebrew/bin` + `/usr/local/bin`. Übernehmen wie `macos/installer/app.py:17-22`.
- `HOTKEY_DISPLAY`: auf Unicode `⌘⇧⌥⌃` umstellen (s. Design-Frage 3).
- `DEFAULT_CONFIG["hotkey"]`: `cmd+shift` (s. Design-Frage 2).
- Paste-Simulation: statt `self._kb_controller.press(Key.ctrl)` + `'v'` → 
  `Key.cmd` + `'v'`. Rest der Logik (Suppress-Listener-Flag gegen Self-Trigger)
  **wichtig beibehalten**.
- STT: wenn `openai-whisper`: `self.model = whisper.load_model(size)`, 
  `self.model.transcribe(audio_or_path, language=lang, fp16=False)`. MPS-
  Nutzung ist in neueren openai-whisper-Versionen automatisch, sonst `device="mps"`.
  Audio kann als numpy-Array rein (wie faster-whisper) oder als WAV-File wie
  die alte Mac-Version — numpy-Array spart den tempfile-Umweg, sollte gehen
  (`whisper.transcribe(model, audio=numpy_float32_array, …)` — API der
  Lower-Level `whisper.transcribe` vs `model.transcribe` prüfen).
- `hotkey_options` im Tray-Submenü anpassen: `["cmd+shift", "cmd", "ctrl", "shift", "alt"]`.
- `Info.plist` für den Build mit `LSUIElement=YES` (oder via PyInstaller-Spec
  setzen). Läuft der App dann headless im Hintergrund, nur Menüleisten-Icon.

### 4. Test aus venv
```bash
source .venv/bin/activate
python app.py
```
Beim ersten Start: Accessibility-Prompt akzeptieren. Menüleisten-Icon erscheint.
Smoke-Test: Hotkey halten, sprechen, loslassen → Text wird ins aktive Fenster
gepastet.

### 5. Build
Sobald der Source-Lauf sauber ist: PyInstaller-Spec erstellen (Analogie zu
`windows/IQspeakr.spec`, aber Mac-Flags). Typisch:
```bash
pyinstaller --name IQspeakr --windowed --onedir \
  --icon ../IQspeakr.icns \
  --osx-bundle-identifier com.iqspeakr.app \
  --collect-all whisper \
  --collect-submodules pynput \
  --exclude-module tkinter \
  app.py
```
Dann `build_dmg.sh` anpassen (Pfad auf `macos_v2/dist/IQspeakr.app`) und laufen
lassen.

### 6. Distribution-Checkliste
- [ ] Code-Signing (optional, mit User klären)
- [ ] DMG baut durch
- [ ] Auf einem anderen Mac installieren + Hotkey testen
- [ ] `config.json` landet in `~/IQspeakr/config.json`, bestehende Version wird
      nicht überschrieben

---

## Was NICHT in `macos_v2/` gehört

- `rumps` — durch Qt ersetzt.
- `AppKit`-Imports außer ggf. für `setActivationPolicy` (als Fallback).
- Der `~/.cache/whisper/*.pt`-Check aus der Alt-Version: `faster-whisper` nutzt
  `~/.cache/huggingface/hub/…`, `openai-whisper` weiter `~/.cache/whisper/`.
  Je nach Entscheidung Frage 1 anpassen.
- Die alte keyCode-Tabelle (`KEYCODE_MAP` mit macOS HIToolbox-Codes) — pynput
  macht das abstraktion-mäßig selbst.

---

## Fallback: wenn der große Rewrite scheitert

Wenn zu viele Mac-Eigenheiten reinkommen, die den Qt-Pfad blockieren
(z.B. Qt kann kein Menüleisten-Icon sauber rendern ohne Dock-Icon), gibt's den
**kleinen Port** als Plan B: Original-Mac-Stack beibehalten, nur die
Feature-Defaults kopieren (hotkey, whisper-model, language, paste-delay,
multi-modifier-hold, recorder-dialog verwerfen oder mit Qt-only-Dialog parallel
einbauen). Siehe Entscheidungs-Gespräch zwischen User und mir vom 2026-04-24 in
der Commit-History.

---

## Relevante Speicherorte zur Laufzeit (auf Mac)

- Source: `~/Projekte/iqspeakr/macos_v2/` (bzw. wo der Clone liegt)
- venv: `macos_v2/.venv/`
- User-Config: `~/IQspeakr/config.json`
- Log: `~/IQspeakr.log`
- Crash-Log (falls faulthandler übernommen): `~/IQspeakr.crash.log`
- Singleton-Lock: `/tmp/iqspeakr.pid`
- Whisper-Cache: `~/.cache/whisper/` (openai-whisper) oder
  `~/.cache/huggingface/hub/` (faster-whisper)

---

## Zum Schluss: User-Kontext

Gaetano sitzt am MacBook, will konkrete Ergebnisse schnell sehen, kommuniziert
knapp. Bevorzugt direkte Ausführung über Vorschläge. Erwartet, dass Claude bei
komplexen Diagnose-Problemen Subagents spawnt. Deutscher Muttersprachler.
Mac-Setup ist Apple Silicon (aus Memory abgeleitet, ggf. verifizieren). Die
DMG-Distribution ging bisher ohne Code-Signing — mit User klären, ob das so
bleiben soll.
