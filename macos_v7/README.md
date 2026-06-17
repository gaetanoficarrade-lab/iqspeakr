# IQspeakr macOS v7

Mac-Variante mit vollem Funktionsumfang von `../windows_v2/`: vier-View-
Hauptfenster (Home/Dashboard/Style/Settings) zusaetzlich zum Tray + Hotkey,
plus alle macOS-26-Stolperstein-Fixes aus v2/v3 (NSStatusItem-Subprocess,
pynput-Monkey-Patch, C-Mach-O-Launcher fuer TCC-Identity, Splash-Bootstrap)
sowie die v5-Erweiterungen (silent in-app Ollama-Install, Eigennamen-
Woerterbuch, deterministischer Launcher-Build fuer stabile TCC-Permissions).

`../macos/` bleibt als stabiles Backup. v2 ist der Tahoe-TCC-Fix, v3 die
Polish-Variante (eingefroren). v7 baut auf v6 auf — Aenderungen ab jetzt
nur hier in `macos_v7/`.

## Was v7 anders macht (Überblick)

v7 ist funktional identisch zu v6 (Aufnahme/Whisper/Hotkey/Paste/Ollama/TCC
unverändert) und ergänzt rein additiv:

1. **Remote-Fehlerreporting (Sentry)** — wir sehen endlich, *warum* die App bei
   einzelnen Usern bricht. Details unten.
2. **Neuer Look** — Light-Theme (warmes Off-White + Teal), Fraunces für
   Headlines, Inter für Body (beide Fonts in `assets/fonts/` mitgeliefert),
   modern-minimal, gefüllte Primär-/outlined Sekundär-Buttons.
3. **Footer auf allen Views** — „by Gaetano Ficarra · Business auf Autopilot"
   (klickbar → Skool) + Versionsanzeige.
4. **Auto-Update-Hinweis** — prüft beim Start im Hintergrund gegen GitHub
   Releases; bei neuerer Version Tray-Hinweis + in den Settings („Über
   IQspeakr") ein Button zur Release-Seite. **Kein** Auto-Download/Install,
   offline still. Voraussetzung: öffentliches Repo `UPDATE_REPO`, Releases als
   `vX.Y.Z` getaggt mit `.dmg`-Asset.
5. **Daten-Backup-Netz** — beim Start rotierende Sicherung (max. 5) von
   `history.json` + `stats.db` nach `~/IQspeakr/backups/`. Die Dashboard-Stats
   liegen ohnehin in `~/IQspeakr/` und werden vom Installer (löscht nur
   `~/.iqspeakr/`) nicht angefasst — das Backup ist zusätzliche Absicherung.
6. **Opt-out-Checkbox** für das Fehlerreporting in den Settings.

### Remote-Fehlerreporting (Sentry)

Damit sehen wir endlich, *warum* die App bei einzelnen Usern bricht, statt blind
zu raten.

- **Nur echte Fehler/Crashes** + Umgebung (macOS-Version, CPU, App-Version)
  gehen an unser Sentry-Projekt in der **EU-Region** (Daten verlassen die EU
  nicht).
- **Keine Transkripte, kein Clipboard, keine getippten Texte.** Die Logging-
  Integration erzeugt bewusst *keine* Breadcrumbs aus Log-Messages
  (`LoggingIntegration(level=None)`), `send_default_pii=False`, plus eine
  defensive `before_send`-Waesche.
- **Opt-out** fuer den User: `~/IQspeakr/config.json` → `"error_reporting": false`,
  oder Umgebungsvariable `IQSPEAKR_NO_TELEMETRY=1`.

### Einmalige Einrichtung (du, vor dem Build)

Sentry meldet nur, wenn eine **DSN** gesetzt ist — sonst ist alles ein No-Op.

1. Account auf sentry.io anlegen, beim Anlegen **Region „EU"** waehlen.
2. Neues Projekt „Python" → die **DSN** kopieren (Form
   `https://<key>@<org>.ingest.de.sentry.io/<project>` — das `.de.` zeigt
   die EU-Region).
3. DSN in `app.py` **fest eintragen** (in `_SENTRY_DSN_BAKED = "..."`), dann
   bauen. Das ist noetig, weil die App beim User per Doppelklick startet — eine
   Umgebungsvariable vom Build-Rechner ist dort nicht vorhanden. Eine Sentry-DSN
   ist kein Geheimnis (erlaubt nur Senden, nicht Lesen), Einbacken ist der
   uebliche Weg fuer Desktop-Apps.
   - Fuer die *Entwicklung* kann man stattdessen `export IQSPEAKR_SENTRY_DSN=...`
     setzen — das ueberschreibt den eingebackenen Wert nur auf dem eigenen Rechner.

> Hinweis: Der Launcher (`launcher.c`) ist unveraendert zu v6 — der cdhash
> bleibt gleich, also behalten Macs ihre TCC-Permissions beim Update v6→v7.

## Funktionsumfang (Basis v6)

- Vollwertiges Hauptfenster mit vier Views (Home, Dashboard, Style, Settings) — erreichbar per Tray-Menu-Eintrag "Hauptfenster oeffnen". Beim Schliessen versteckt sich das Fenster, App lebt im Tray weiter.
- Aufnahme-History (letzte 10 Transkripte) und Dashboard mit Streak / WPM / Aktivitaets-Heatmap, persistiert in `~/IQspeakr/history.json` und `~/IQspeakr/stats.db` (SQLite).
- Style-Presets fuer KI-Textbereinigung: Foermlich / Locker / Sehr locker / Individuell (mit Checkbox-Builder). Ollama wird seit v5 silent in-app installiert (Download von GitHub Releases nach `~/.iqspeakr/bin/`, kein Tray, kein Browser-Detour).

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
cd macos_v7
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
- `IQspeakr-v7-Installer.dmg` — verteilbar (DMG enthaelt einen kleinen
  Bash-Installer, der Python-Standalone, ffmpeg, venv und das App-Bundle
  beim ersten Lauf einrichtet)

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
