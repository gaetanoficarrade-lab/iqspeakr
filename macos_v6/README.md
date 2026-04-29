# IQspeakr — macOS

Sprache-zu-Text-App, die komplett lokal auf deinem Mac läuft. Hotkey drücken, sprechen, loslassen — der Text wird transkribiert und automatisch ins gerade aktive Programm eingefügt (per `Cmd+V`).

Diktat funktioniert in **jeder** App: Mail, Browser, Xcode, Safari, Notes, Slack, Terminal. Kein Cloud-Service, kein Account, keine Internetverbindung nötig (außer einmalig beim ersten Start zum Whisper-Modell-Download).

---

## Was ist IQspeakr und wofür ist die App?

**Kurz:** Du sprichst, IQspeakr schreibt. Lokal, schnell, ohne dass deine Sprache jemals dein Gerät verlässt.

**Wann macht das Sinn?**

- Du tippst viel und willst längere Texte schneller produzieren — Sprechen ist 3× schneller als Tippen.
- Du willst keine Cloud-Diktat-Apps (z.B. Wispr Flow) verwenden, weil deine Daten privat bleiben sollen.
- Du arbeitest an Texten, in denen sensible Inhalte vorkommen (Klienten-Notizen, Patentanwalt-Schriftsätze, Gesundheitsdaten, Code-Kommentare mit internen API-Schlüsseln).
- Du willst auch offline diktieren können (z.B. im Zug, im Flugzeug).
- Du bist Software-Entwickler / Wissensarbeiter / Berater und willst E-Mails, Tickets, Commit-Messages oder Doku-Texte per Stimme entwerfen.

**Was IQspeakr NICHT ist:**

- Keine Echtzeit-Live-Transkription wie z.B. Untertitel im Meeting (die App nimmt einen kompletten Block auf, transkribiert dann am Stück).
- Kein universeller Sprachassistent — sie führt keine Befehle aus, sie schreibt nur Text.
- Keine Diktat-Software für lange Diktate (Anwalt, Arzt) mit Diktathandschriften — eher der schnelle Inline-Use.

---

## Wie es im Alltag funktioniert (in 30 Sekunden)

1. **Hotkey halten** — Standard ist `Strg + Shift` halten. Während du hältst, läuft die Aufnahme.
2. **Sprich deinen Satz** — z.B. *„Antwort an Lisa, ich kann morgen leider nicht teilnehmen, melde mich Montag wieder"*.
3. **Hotkey loslassen** — die App transkribiert (1–3 Sekunden je nach Modell), bereinigt den Text optional per KI, und fügt ihn automatisch dort ein, wo dein Cursor steht.

In der **Menüleiste** oben rechts siehst du den Status: grau = Bereit, rot = Aufnahme läuft, orange = wird verarbeitet.

**Hotkey-Modi** (für reine Modifier-Kombos wie `Strg+Shift`):

| Aktion | Was passiert |
|---|---|
| Lange halten | Aufnahme läuft solange du hältst |
| Kurz tippen | Kurze Aufnahme (~2 Sek), praktisch für Einzelwörter |
| 2× tippen | Daueraufnahme bis du nochmal tippst — gut für lange Diktate |

Auf dem Mac kannst du alternativ `Cmd` halten als Hotkey verwenden — Default ist aber `Strg + Shift` für Konsistenz mit Windows.

---

## Spracherkennung (Whisper)

IQspeakr nutzt **OpenAI Whisper**, das komplett lokal auf deinem Mac läuft. Auf Apple Silicon (M1/M2/M3/M4) wird automatisch die GPU (MPS) genutzt — deutlich schneller als CPU. Auf Intel-Macs läuft es auf der CPU.

Beim ersten Start wird das Modell einmalig heruntergeladen, danach offline-fähig.

**Modell-Größen** (umstellbar in *Settings → Whisper-Modell*):

| Modell | Größe | Geschwindigkeit (M-Chip) | Qualität | Empfohlen für |
|---|---|---|---|---|
| `tiny` | ~75 MB | Sehr schnell | Ungenau | Schnellste Reaktion |
| `base` | ~145 MB | Schnell (~0.5s) | Gut | **Default für die meisten** |
| `small` | ~465 MB | Mittel (~1s) | Sehr gut | Wenn `base` zu viele Fehler macht |
| `medium` | ~1.5 GB | Etwas langsamer (~2s) | Beste | Lange Aufnahmen, Fachbegriffe |

**Praxis-Tipps:**

- Auf Apple Silicon ist sogar `medium` flüssig nutzbar.
- Bei viel Fachvokabular (medizinisch, juristisch, Code-Begriffe) → `small`.
- Sprache ist standardmäßig auf Deutsch gesetzt. Auf Englisch umstellen verdoppelt manchmal die Genauigkeit für englische Texte.

**Hardware-Faustregel:**
- Apple Silicon (M1+): Whisper läuft über MPS-Backend auf der integrierten GPU. Sehr ressourcenschonend.
- Intel-Mac: läuft auf der CPU, ist deutlich langsamer. Eher `base` als Maximum.

---

## KI-Textbereinigung (Cleanup) — der Knackpunkt

Beim Diktieren entstehen zwangsläufig Füllwörter (*„äh, also, halt, irgendwie"*), Wortdoppelungen (*„ich ich"*), fehlende Satzzeichen und kleine Grammatikholper. Whisper liefert das alles 1:1 wie gesprochen. Cleanup räumt das auf — falls aktiviert.

### Wann ist Cleanup aktiv?

**Cleanup läuft NUR wenn beide Bedingungen erfüllt sind:**

1. *Ollama-Backend* ist installiert und läuft auf `localhost:11434`
2. *Cleanup* ist im Settings-Tab aktiviert

Wenn eine der beiden Bedingungen nicht stimmt, kommt der Whisper-Rohtext direkt durch — ohne Bereinigung. Du diktierst dann roh, mit allen Füllwörtern.

### Was Cleanup automatisch macht

Je nach gewähltem **Style-Preset** (Settings → Style):

| Style | Was passiert |
|---|---|
| **Locker** (Default) | Füllwörter weg, Doppler weg, Satzzeichen + Großschreibung, leichte Grammatik. Stil bleibt umgangssprachlich. |
| **Förmlich** | Wie *Locker*, plus: Umgangssprache wird zu Schriftdeutsch (*„kriegen"* → *„erhalten"*, *„geht's"* → *„geht es"*), Sätze werden für Lesbarkeit umgestellt. |
| **Sehr locker** | Nur Fülllaute (*„ähm, äh, mhm"*) entfernen. Sonst NICHTS ändern. Stilworte wie *„halt"* und *„also"* bleiben drin. |
| **Individuell** | Du baust dir per Checkbox-Liste deinen eigenen Cleanup-Profil zusammen, plus optional eigene Anweisungen (z.B. *„Behandle englische IT-Begriffe wie 'API' großgeschrieben"*). |

### Was Cleanup NIE machen sollte

- Inhaltlich Sätze hinzufügen oder weglassen
- Auf Fragen im Diktat antworten (*„fixe den Bug"* wird NICHT zu *„Hier ist eine Anleitung..."*)
- Zusammenfassen oder kürzen

### Trade-off: Cleanup kostet Zeit

Cleanup pro Diktat:

| Modell (Settings → Ollama-Modell) | Geschwindigkeit (M-Chip) | Qualität / Robustheit |
|---|---|---|
| `llama3.2:1b` (Default) | <1 Sekunde | Solide für normale Sätze. **Schwächt** bei imperativen Diktaten („*Fixe das mal*") — kann in einen Refusal-Modus kippen. |
| `llama3.2:latest` (3B) | 1–2 Sekunden | Robuster, hält die Cleanup-Rolle auch bei imperativen Eingaben. |
| `llama3.1` (8B) | 3–5 Sekunden | Beste Qualität. Auf M-Chips noch praktikabel; auf Intel zu langsam. |

**Wenn du häufig den Verdacht hast „dieses Cleanup-Output ist Quark"** — das `1b`-Modell ist zu klein für komplexe Diktate. Wechsel auf `llama3.2:latest` (3B).

### Wann Cleanup eher AUSGESCHALTET lassen?

- Wenn du in Xcode oder einer IDE Code-Kommentare diktierst und Cleanup deine Code-Begriffe zerstört.
- Wenn dein Mac ein älterer Intel-Mac ist und du Lüftergeräusche minimieren willst.
- Wenn du sehr kurze, klare Phrasen diktierst (*„ja"*, *„nein"*, *„passt"*) — keine Notwendigkeit zu bereinigen.

Im Tray-Menü kannst du Cleanup jederzeit per Klick togglen.

---

## Eigennamen-Wörterbuch

Whisper kennt deine Firma, Projektnamen oder besondere Eigenschreibweisen nicht. Wenn du regelmäßig *„IQspeakr"* sagst, hört Whisper *„IQ Speaker"* oder *„IQ-Speaker"*.

Im Tab **Wörterbuch** kannst du **Korrekturpaare** anlegen:

```
Korrekt: IQspeakr
Varianten: IQ Speaker, IQ-Speaker, IQs Bikker
```

Die App ersetzt vor dem Cleanup automatisch alle Varianten durch die korrekte Schreibweise. Zusätzlich wird der Korrekt-Name dem Cleanup-Modell als „behalte exakt"-Hinweis mitgegeben — damit Ollama deine Eigennamen nicht heimlich umformatiert.

Das Wörterbuch ist persistiert in `~/Library/Application Support/IQspeakr/dictionary.json`.

---

## Datenschutz / Lokal-Verarbeitung

- **Audio verlässt deinen Rechner nicht.** Whisper läuft im Prozess der App.
- **Cleanup läuft lokal über Ollama.** Auch hier: kein API-Call, keine Cloud.
- **Einzige Internetverbindung** ist beim ersten Start zum Whisper-Modell-Download (kommt von HuggingFace) und beim ersten Mal beim Ollama-Modell-Pull. Danach offline einsetzbar.
- **Was die App speichert** (alles lokal): Konfiguration, History der letzten 10 Transkripte (für die Home-Ansicht), Aktivitäts-Statistiken (Streak, WPM, Heatmap) in einer SQLite-DB.

Keine Telemetrie. Keine Crash-Reports an externe Server. Logs landen ausschließlich in `~/IQspeakr.log` lokal.

---

## Performance-Hinweise

| Aktion | Typische Zeit (M-Chip) | Was beeinflusst es |
|---|---|---|
| App-Start | 2–4 Sek | Erstes Mal: Whisper-Modell laden. Folgemal: schneller. |
| Aufnahme | Echtzeit | Mikrofonabhängig — bei Bluetooth-Headsets ggf. Latenz |
| Whisper-Transkription | 0.3× bis 2× Audiolänge | MPS auf M-Chips deutlich schneller als Intel-CPU |
| Cleanup (Ollama) | <1 bis 5 Sek | Modellgröße, RAM |
| Auto-Paste | <100 ms | Quartz CGEvent → Cmd+V |

**Wenn die App spürbar lahm ist:** Whisper-Modell auf `tiny` oder `base`, Ollama-Modell auf `1b` umstellen, oder Cleanup ganz aus.

---

## Erster Start mit dem Installer

1. **`IQspeakr-v6-Installer.dmg`** doppelklicken.
2. Bash-Installer im DMG ausführen — richtet beim ersten Lauf Python-Standalone, ffmpeg, venv und das `.app`-Bundle in `~/Applications/IQspeakr.app` ein.
3. App starten.
4. **macOS fragt nach drei Permissions** beim ersten Mal:
   - **Bedienungshilfen** (für globalen Hotkey via pynput)
   - **Input Monitoring** (zusätzlich auf neueren macOS-Versionen)
   - **Mikrofon-Zugriff** (beim ersten Aufnahme-Versuch)
5. Alle drei in *Systemeinstellungen → Datenschutz & Sicherheit* freigeben, App einmal neu starten.
6. Beim ersten Diktat lädt Whisper das `base`-Modell (~145 MB).

**LSUIElement=true** im Bundle: die App hat **kein Dock-Icon**, sie lebt komplett in der Menüleiste oben. Über das Menüleisten-Icon → *„Hauptfenster öffnen"* kommst du an die Tabs.

**Ollama-Setup** (für Cleanup, optional): seit v5 wird Ollama silent in-app installiert. Im Hauptfenster → Settings → Cleanup einrichten klicken — die App lädt Ollama nach `~/.iqspeakr/bin/`, kein Browser, kein Tray-Icon, kein extra App-Bundle. Wenn du das nicht brauchst: einfach ignorieren, App läuft auch ohne Cleanup vollständig.

---

## Hauptfenster-Tabs im Überblick

- **Home** — Letzte 10 Transkripte mit Copy-Button. Praktisch um auf etwas zurückzugreifen, das du gerade diktiert hast.
- **Dashboard** — Streak (Tage in Folge mit Diktaten), Wörter pro Minute, Aktivitäts-Heatmap der letzten Wochen.
- **Style** — Cleanup-Stil-Auswahl mit Live-Vorher/Nachher-Beispielen.
- **Wörterbuch** — Eigennamen-Korrekturen anlegen/bearbeiten.
- **Settings** — Hotkey, Whisper-Modell, Sprache, Ollama-Modell, Overlay-Toggle, Notification-Toggle.

Hauptfenster wird beim Schließen versteckt, App lebt im Tray weiter. Menüleisten-Icon-Klick öffnet das Hauptfenster wieder.

---

## Häufige Stolperfallen (macOS-spezifisch)

- **Hotkey reagiert nicht**: In Systemeinstellungen → Datenschutz & Sicherheit → **Bedienungshilfen** UND **Input Monitoring** beide für `IQspeakr.app` aktiviert? Beide nötig auf macOS Sonoma+.
- **Permissions plötzlich weg nach App-Update**: macOS bindet TCC-Permissions an die Code-Signature. Wir bauen den Launcher deterministisch (cdhash byte-identisch über Updates), damit dein „Bedienungshilfen ja" über Updates erhalten bleibt. Falls trotzdem weg: einmal in Systemeinstellungen die Toggle aus- und wieder einschalten.
- **Hotkey kollidiert mit System-Shortcuts**: `Cmd allein` kollidiert mit Cmd-Tab. Wir empfehlen `Strg + Shift` halten (cross-platform konsistent).
- **App startet nicht aus DMG**: macOS Gatekeeper. Beim ersten Mal Rechtsklick → Öffnen, oder in den Sicherheits-Einstellungen freigeben.
- **Quarantine-xattr-Bug auf macOS Tahoe (26.4)**: Wenn `CGEventTap` silent fehlschlägt, hat der Launcher das `com.apple.quarantine`-xattr noch dran. v6 entfernt das xattr beim ersten Start automatisch.

---

## Für Entwickler (Build / Hacken)

### Voraussetzungen

- Apple Silicon Mac (Intel geht auch, dann CPU statt MPS)
- macOS 12+
- Python 3.11+: `brew install python@3.12` oder python.org
- ffmpeg: `brew install ffmpeg`
- Optional: Ollama für KI-Textbereinigung — `brew install ollama && ollama serve`

### Aus Source starten

```bash
cd macos_v6
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Build (.app + DMG)

```bash
cd macos_v6
./build_dmg.sh
```

Ergebnis:
- `IQspeakr-v6-Installer.dmg` — verteilbar (DMG enthält einen kleinen Bash-Installer, der Python-Standalone, ffmpeg, venv und das App-Bundle beim ersten Lauf einrichtet)

Der `Info.plist` bekommt `LSUIElement=true` (kein Dock-Icon).

### Konfig + Datenpfade

- `~/IQspeakr/config.json` — App-Settings (Hotkey, Whisper-Modell, Sprache, Ollama-Modell, Style etc.)
- `~/Library/Application Support/IQspeakr/dictionary.json` — Wörterbuch
- `~/Library/Application Support/IQspeakr/history.json` — Letzte 10 Transkripte
- `~/Library/Application Support/IQspeakr/stats.db` — SQLite-DB für Dashboard
- `~/IQspeakr.log` — App-Log (DEBUG)
- `~/IQspeakr.crash.log` — Crash-Log
- `/tmp/iqspeakr.pid` — Singleton-Lock (`fcntl.flock`)
- `~/.cache/whisper/*.pt` — Whisper-Modell-Cache
- `~/.iqspeakr/bin/ollama` — silent installiertes Ollama-Backend (seit v5)

### Stack

| Aspekt | Komponente |
|---|---|
| GUI | PySide6 (Qt) + NSStatusItem-Subprocess für Tray |
| STT | openai-whisper mit MPS auf Apple Silicon |
| Hotkey | pynput.keyboard.Listener + keycode_context-Monkey-Patch |
| Paste | Quartz CGEvent → Cmd+V |
| Clipboard | `pbcopy` |
| Singleton | `fcntl.flock` auf `/tmp/iqspeakr.pid` |
| Build | C-Mach-O-Launcher + Python-Standalone, deterministischer Build für TCC-Stabilität |
| Cleanup-Backend | Ollama (HTTP localhost:11434) |

### Versionen-Linien

- `../macos/` — Legacy, eingefroren
- `../macos_v2/` — Tahoe-TCC-Fix
- `../macos_v3/` — Polish, eingefroren
- `../macos_v4/` — Hauptfenster mit 4 Views, bidirektionale Settings-Sync
- `../macos_v5/` — Silent in-app Ollama-Install, Wörterbuch
- `macos_v6/` ← aktive Linie. Quarantine-xattr-Fix, deterministischer Launcher-Build (cdhash byte-identisch zu v5)

Änderungen ab jetzt nur hier in `macos_v6/`.
