# IQspeakr — Windows

Sprache-zu-Text-App, die komplett lokal auf deinem Rechner läuft. Hotkey drücken, sprechen, loslassen — der Text wird transkribiert und automatisch ins gerade aktive Programm eingefügt (per `Ctrl+V`).

Diktat funktioniert in **jedem** Eingabefeld: E-Mail, Browser, IDE, Word, Slack, Notes-App. Kein Cloud-Service, kein Account, keine Internetverbindung nötig (außer einmalig beim ersten Start zum Whisper-Modell-Download).

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

Im Tray-Icon (rechts unten neben der Uhr) siehst du den Status: grau = Bereit, rot = Aufnahme läuft, orange = wird verarbeitet.

**Hotkey-Modi** (für reine Modifier-Kombos wie `Strg+Shift`):

| Aktion | Was passiert |
|---|---|
| Lange halten | Aufnahme läuft solange du hältst |
| Kurz tippen | Kurze Aufnahme (~2 Sek), praktisch für Einzelwörter |
| 2× tippen | Daueraufnahme bis du nochmal tippst — gut für lange Diktate |

---

## Spracherkennung (Whisper)

IQspeakr nutzt **OpenAI Whisper** (Variante `faster-whisper`), das komplett lokal auf deinem Rechner läuft. Beim ersten Start wird das Modell einmalig heruntergeladen, danach offline-fähig.

**Modell-Größen** (umstellbar in *Settings → Whisper-Modell*):

| Modell | Größe | Geschwindigkeit | Qualität | Empfohlen für |
|---|---|---|---|---|
| `tiny` | ~75 MB | Sehr schnell | Ungenau | Alte/schwache Hardware |
| `base` | ~145 MB | Schnell (~1s) | Gut | **Default für die meisten** |
| `small` | ~465 MB | Mittel (~2s) | Sehr gut | Wenn `base` zu viele Fehler macht |
| `medium` | ~1.5 GB | Langsam (~4s) | Beste | Lange Aufnahmen, Fachbegriffe |

**Praxis-Tipps:**

- Auf einem normalen Laptop (i5/Ryzen 5+) reicht `base` für 95% aller Fälle.
- Bei viel Fachvokabular (medizinisch, juristisch, Code-Begriffe) → `small`.
- `medium` ist nur sinnvoll wenn du regelmäßig längere Diktate machst und die zusätzliche Wartezeit akzeptierst.
- Sprache ist standardmäßig auf Deutsch gesetzt. Auf Englisch umstellen verdoppelt manchmal die Genauigkeit für englische Texte.

**Hardware-Faustregel:** Whisper läuft komplett auf der CPU (nicht GPU). Mehr CPU-Kerne = mehr parallele Audio-Verarbeitung = schneller. Die App nutzt INT8-Quantisierung — sehr ressourcenschonend.

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

| Modell (Settings → Ollama-Modell) | Geschwindigkeit | Qualität / Robustheit |
|---|---|---|
| `llama3.2:1b` (Default) | ~1 Sekunde | Solide für normale Sätze. **Schwächt** bei imperativen Diktaten („*Fixe das mal*") — kann in einen Refusal-Modus kippen. |
| `llama3.2:latest` (3B) | ~2–3 Sekunden | Robuster, hält die Cleanup-Rolle auch bei imperativen Eingaben. |
| `llama3.1` (8B) | ~5–7 Sekunden | Beste Qualität, aber spürbar langsamer. Macht Sinn wenn du oft diktierst und Wert auf perfekte Bereinigung legst. |

**Wenn du häufig den Verdacht hast „dieses Cleanup-Output ist Quark"** — das `1b`-Modell ist zu klein für komplexe Diktate. Wechsel auf `llama3.2:latest` (3B).

### Wann Cleanup eher AUSGESCHALTET lassen?

- Wenn du in einer IDE Code-Kommentare diktierst und Cleanup deine Code-Begriffe zerstört.
- Wenn dein Rechner schwach ist (alter Laptop, kein RAM) — Ollama braucht 2-4 GB RAM für ein 3B-Modell.
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

Das Wörterbuch ist persistiert in `%APPDATA%\IQspeakr\dictionary.json`.

---

## Datenschutz / Lokal-Verarbeitung

- **Audio verlässt deinen Rechner nicht.** Whisper läuft im Prozess der App.
- **Cleanup läuft lokal über Ollama.** Auch hier: kein API-Call, keine Cloud.
- **Einzige Internetverbindung** ist beim ersten Start zum Whisper-Modell-Download (kommt von HuggingFace) und beim ersten Mal beim Ollama-Modell-Pull. Danach offline einsetzbar.
- **Was die App speichert** (alles lokal): Konfiguration, History der letzten 10 Transkripte (für die Home-Ansicht), Aktivitäts-Statistiken (Streak, WPM, Heatmap) in einer SQLite-DB.

- **Fehlerberichte (opt-out):** Bei echten Fehlern/Crashes sendet die App einen Bericht an ein Sentry-Projekt in der **EU-Region** — **nur** Fehlertyp + Umgebung (Windows-Version, CPU, App-Version). **Keine Transkripte, kein Clipboard, kein getippter Text, keine Audiodaten.** Abschaltbar in *Settings → „Anonyme Fehlerberichte senden"* oder per `config.json → "error_reporting": false`. Ohne konfigurierte DSN passiert ohnehin nichts. Logs landen lokal in `~/IQspeakr.log`.

---

## Performance-Hinweise

| Aktion | Typische Zeit | Was beeinflusst es |
|---|---|---|
| App-Start | 2–8 Sek | Erstes Mal: Whisper-Modell laden (RAM). Folgemal: schnell. |
| Aufnahme | Echtzeit | Mikrofonabhängig — bei Bluetooth-Headsets ggf. Latenz |
| Whisper-Transkription | 1× bis 4× Audiolänge⁻¹ | CPU-abhängig, Modellgröße |
| Cleanup (Ollama) | 1–7 Sek | Modellgröße, RAM, CPU |
| Auto-Paste | <100 ms | Per `pynput` simuliertes Ctrl+V |

**Wenn die App spürbar lahm ist:** Whisper-Modell auf `tiny` oder `base`, Ollama-Modell auf `1b` umstellen, oder Cleanup ganz aus. Auf Office-Rechnern ohne GPU reicht das meistens für flüssiges Diktat.

---

## Erster Start mit dem Installer

1. **`IQspeakr-Setup.exe`** doppelklicken.
2. Wizard durchlaufen — installiert per-User unter `%LOCALAPPDATA%\Programs\IQspeakr\` (kein Admin-Rechte nötig).
3. Optional: Desktop-Verknüpfung anhaken, Autostart anhaken.
4. Beim ersten App-Start lädt Whisper das `base`-Modell (~145 MB) — einmalig, im Hintergrund.
5. Standard-Hotkey ist `Strg + Shift` halten. Im **Settings-Tab** änderbar.

**Beim ersten Diktat-Versuch** fragt Windows nach Mikrofon-Berechtigung. Annehmen, fertig.

**Ollama-Setup** (für Cleanup, optional): Das Tray-Menü hat einen Eintrag *„Cleanup einrichten"* — die App lädt Ollama silent in den User-Ordner und installiert sich selbst das `llama3.2:1b`-Modell. Kein Browser, kein extra Klick. Wenn du das nicht brauchst: einfach ignorieren, App läuft auch ohne Cleanup vollständig.

---

## Hauptfenster-Tabs im Überblick

- **Home** — Letzte 10 Transkripte mit Copy-Button. Praktisch um auf etwas zurückzugreifen, das du gerade diktiert hast.
- **Dashboard** — Streak (Tage in Folge mit Diktaten), Wörter pro Minute, Aktivitäts-Heatmap der letzten Wochen.
- **Style** — Cleanup-Stil-Auswahl mit Live-Vorher/Nachher-Beispielen.
- **Wörterbuch** — Eigennamen-Korrekturen anlegen/bearbeiten.
- **Settings** — Hotkey, Whisper-Modell, Sprache, Ollama-Modell, Overlay-Toggle, Notification-Toggle.

Hauptfenster wird beim Schließen versteckt, App lebt im Tray weiter. Tray-Icon-Doppelklick öffnet das Hauptfenster wieder.

---

## Häufige Stolperfallen (Windows-spezifisch)

- **Hotkey reagiert nicht**: Antivirus blockiert globale Keyboard-Hooks. Ausnahme für `IQspeakr.exe` setzen.
- **`Ctrl` allein** kollidiert mit Shortcuts (Kopieren etc.). Auf `Strg + Shift` halten umstellen oder eigene Kombi.
- **Mikrofon liefert 0 Frames**: USB-Audio-Endpoint-Hang. Mikrofon einmal physisch aus-/einstecken oder PC-Reboot. Häufig bei Trust GXT-Headsets.
- **Toast-Benachrichtigungen erscheinen nicht**: Windows-Fokus-Assistent ist an.
- **App startet nicht zweimal**: Singleton-Lock verhindert Mehrfach-Instanzen. Stale-Lock-Datei in `%TEMP%\iqspeakr.lock` wird automatisch geräumt.

---

## Für Entwickler (Build / Hacken)

### Voraussetzungen

- Windows 10 oder 11 (64 Bit)
- Python 3.12 — `winget install Python.Python.3.12`
- Optional: **Ollama** für KI-Textbereinigung — `winget install Ollama.Ollama` oder <https://ollama.com/download/windows>

### Aus Source starten

```powershell
cd windows_v2026.6.0
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

### Build (.exe + Setup.exe)

```powershell
cd windows_v2026.6.0
.\build_installer.ps1 -Rebuild
```

Voraussetzung: **Inno Setup 6** installiert (`winget install JRSoftware.InnoSetup`).

Ergebnis:
- `dist\IQspeakr\` — PyInstaller `--onedir`-Bundle (~300 MB)
- `dist\IQspeakr-Setup.exe` — Installer-Wizard (~85 MB)

Verteilen: nur die `Setup.exe`-Datei verschicken.

### Konfig + Datenpfade

- `~/IQspeakr/config.json` — App-Settings (Hotkey, Whisper-Modell, Sprache, Ollama-Modell, Style etc.)
- `%APPDATA%\IQspeakr\dictionary.json` — Wörterbuch
- `%APPDATA%\IQspeakr\history.json` — Letzte 10 Transkripte
- `%APPDATA%\IQspeakr\stats.db` — SQLite-DB für Dashboard
- `~/IQspeakr.log` — App-Log (DEBUG)
- `~/iqspeakr_v3_session.log` — stderr-Output (Qt-Slot-Exceptions landen NUR hier!)
- `%TEMP%\iqspeakr.lock` — Singleton-Lock
- `~/.cache/huggingface/hub/` — Whisper-Modell-Cache

### Stack

| Aspekt | Komponente |
|---|---|
| GUI | PySide6 (Qt) |
| STT | faster-whisper (CTranslate2) |
| Hotkey | pynput.keyboard.Listener |
| Paste | pynput.Controller (Ctrl+V Sim) |
| Clipboard | pyperclip |
| Singleton | msvcrt.locking auf `%TEMP%\iqspeakr.lock` |
| Build | PyInstaller `--onedir` + Inno Setup |
| Cleanup-Backend | Ollama (HTTP localhost:11434) |
