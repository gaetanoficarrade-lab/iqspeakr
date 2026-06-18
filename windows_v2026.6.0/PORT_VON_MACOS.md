# Windows-Portierung: macOS 2026.6.1 → 2026.6.5 nachziehen

**Stand:** macOS ist auf **2026.6.5**, Windows noch auf **2026.6.0**.
Dieses Dokument listet ALLES auf, was im macOS-`app.py` zwischen 2026.6.1 und
2026.6.5 geändert wurde, plus die genaue Windows-Anpassung je Punkt.

Quelle der Wahrheit ist `macos_v2026.6.0/app.py` (die einzelnen Commits siehe
unten). Beim Portieren: dort die Funktion lesen, hier die Windows-Abweichung
beachten, in `windows_v2026.6.0/app.py` einbauen.

## Commit-Historie macOS (zum Nachschlagen)
- `9352ff1` v2026.6.1 — Phantom-Filter, Cloud-API, Wörterbuch-Auto-Lernen, Sentry-Sichtbarkeit
- `a9f745a` v2026.6.2 — schnellere Cloud-API-Modelle + Update lädt DMG direkt
- `72f3740` (6.2) — Auto-Lernen robuster: Polling statt Single-Check
- `d266532` v2026.6.3 — Auto-Lernen wartet auf fertige Korrektur (Stabilität)
- `cbba6a0` v2026.6.4 — Diagnose-Versand (PIN) + Cloud-API-Fehler sichtbar
- `4b984f3` v2026.6.5 — **Groq User-Agent-Fix (Cloudflare 1010)** ← kritisch

---

## WICHTIGE Plattform-Unterschiede (immer beachten)
| Thema | macOS | Windows |
|------|-------|---------|
| Whisper-Engine | openai-whisper (`result=dict`, `result["segments"]`, `no_speech_prob`) | **faster-whisper** (`segments, info = model.transcribe(...)`, `vad_filter=True`) |
| Paste | Cmd+V (`Key.cmd`) | **Ctrl+V** (`Key.ctrl`) — schon vorhanden |
| Fokusfeld lesen (Auto-Lernen) | Accessibility-API (`AXUIElementCopyAttributeValue`) | **UI Automation** (pywin32/comtypes) — KOMPLETT anders, siehe unten |
| Rechte/TCC | Eingabeüberwachung + Bedienungshilfen | kein TCC → Listener-Watchdog/Debounce/Permission-Wizard ENTFÄLLT |
| Pfade | `~/.iqspeakr`, `~/IQspeakr.log` | `%APPDATA%\IQspeakr`, `~/IQspeakr.log` (Log identisch über `Path.home()`) |
| Update-Asset | `.dmg` | `.exe` (`RELEASE_ASSET_SUFFIX` ist schon `.exe`) |
| Build | `build_dmg.sh` + Launcher/codesign | `build_exe.ps1` + `installer.iss` (Inno) |
| Sentry-Tag | `platform_variant="macos"` | `platform_variant="windows"` (schon gesetzt) |

---

## 0. KRITISCH ZUERST: Groq User-Agent (Cloudflare 1010) — v2026.6.5
**Ohne das schlägt die Groq-API auf Windows GENAUSO fehl.** Groq sitzt hinter
Cloudflare, das den urllib-Default-User-Agent (`Python-urllib/x`) mit Fehler
1010 → HTTP 403 sperrt. Bewiesen: ohne UA → 403/1010, mit UA → sauberes 401.

**Portieren:**
- Konstante `API_USER_AGENT = f"IQspeakr/{__version__} (Windows)"` neben `API_PROVIDERS`.
- `req.add_header("User-Agent", API_USER_AGENT)` in ALLEN drei API-Requests:
  `_multipart_post` (Transkription), `cleanup_via_api` (Chat), `verify_api_key` (Test).
- (urllib verhält sich auf Windows identisch — gleicher Fix.)

---

## 1. Phantom-Text-Filter (Whisper-Halluzinationen) — v2026.6.1
Verhindert erfundenen Text („SWR 2020", „Untertitel", „Vielen Dank") bei
kurzem/leerem Tastendruck.

**Platform-agnostisch (1:1 kopieren):** Konstanten `MIN_SPEECH_DURATION`,
`SILENCE_RMS_THRESHOLD`, `SILENCE_PEAK_THRESHOLD`, `HALLUCINATION_PHRASES`;
Funktionen `_normalize_phrase`, `audio_stats`, `is_probably_silence`,
`looks_like_hallucination`.

**In `_transcribe_frames` einbauen:**
- Stufe 1 (vor Transkription): `if is_probably_silence(audio_data, SAMPLE_RATE): return`
- Stufe 2b (nach Transkription): `if looks_like_hallucination(raw_text, duration_sec): raw_text = ""`

**Windows-Abweichung:** Stufe 2a (`_all_segments_silent`) ist macOS-spezifisch
(openai-whisper `result["segments"]` mit `no_speech_prob`). faster-whisper hat
bereits `vad_filter=True` (filtert Stille) — Stufe 2a entweder weglassen ODER
über `info`/`segments[].no_speech_prob` der faster-whisper-Segmente nachbauen.
Stufe 1 + 2b reichen aber als Hauptschutz.

---

## 2. Cloud-Spracherkennung per API (Groq/OpenAI) — v2026.6.1 + 6.2
**Platform-agnostisch (1:1 kopieren):** `API_PROVIDERS`, `_audio_to_wav_bytes`,
`_http_error_body`, `_multipart_post`, `transcribe_via_api`, `cleanup_via_api`,
`verify_api_key`. **Modelle (Speed-Default, v6.2):** Groq
`whisper-large-v3-turbo` + `llama-3.1-8b-instant`; OpenAI `gpt-4o-mini-transcribe`
+ `gpt-4o-mini`. **User-Agent nicht vergessen (Punkt 0).**

**App-Methoden:** `_api_key()`, `_api_active()`, `cleanup_available()` auf
`IQspeakrApp`. In `_transcribe_frames` Routing einbauen: wenn `_api_active()` →
`transcribe_via_api(...)`, bei Exception Log + Sentry + EINMALIGE Notification
(`self._api_error_notified`) + Fallback auf lokales faster-whisper.

**Config-Defaults ergänzen:** `api_enabled=False`, `api_provider="groq"`,
`api_key_groq=""`, `api_key_openai=""`, `dict_autolearn=True`.

**Cleanup via API (v6.1):** `_cleanup_text` so umbauen, dass es bei aktiver API
über `cleanup_via_api` läuft (sonst Ollama, sonst Rohtext). `toggle_cleanup` und
`StyleView.refresh_lock` auf `self.app.cleanup_available()` umstellen (API ODER
Ollama schaltet Style/Cleanup frei).

---

## 3. Wörterbuch-Auto-Lernen — v2026.6.1, 6.2, 6.3  ← GRÖSSTE Windows-Arbeit
Erkennt eine manuelle Ein-Wort-Korrektur direkt nach dem Einfügen und lernt sie
ins Wörterbuch (variante→korrekt).

**Platform-agnostisch (kopieren):** `_single_word_correction`,
`_is_learnable_word`, `_AUTOLEARN_STOPWORDS`, `_AUTOLEARN_POLL_INTERVAL`,
`_AUTOLEARN_WINDOW`, `_AUTOLEARN_STABLE_POLLS`, `_autolearn_commit`, sowie die
Logik in `_autolearn_poll` (Stabilitäts-Debounce: erst auswerten wenn das Feld
~2,4 s unverändert ist; Vergleich des FERTIGEN Stands gegen v0; Zwischenstände
ignorieren). State-Init in `__init__`: `_autolearn_pending=None`, `_autolearn_token=0`.
Signal `autolearn_sig = Signal(str)` + connect zu `_autolearn_begin` (Main-Thread).
`_arm_autolearn(text)` aus `_transcribe_frames` nach dem Paste aufrufen.

**Windows-Abweichung (das Feld auslesen):** macOS nutzt
`AXUIElementCreateSystemWide()` + `AXUIElementCopyAttributeValue(el, "AXValue")`.
Auf Windows gibt es das NICHT. Ersatz = **UI Automation**:
```python
# pip: comtypes  (oder pywinauto/uiautomation)
import comtypes.client
uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}",
        interface=...)  # IUIAutomation
focused = uia.GetFocusedElement()
# Wert: ValuePattern.CurrentValue  ODER  TextPattern.DocumentRange.GetText(-1)
```
Empfehlung: Bibliothek **`uiautomation`** (pip `uiautomation`) — viel einfacher:
```python
import uiautomation as auto
ctrl = auto.GetFocusedControl()
val = ctrl.GetValuePattern().Value   # bei Editier-Controls
# Fallback: ctrl.GetTextPattern().DocumentRange.GetText(-1)
```
Nur `_ax_focused_value()` (liefert `(element, text)`) und das Re-Lesen in
`_autolearn_poll` müssen auf UIA umgeschrieben werden — die restliche Poll-Logik
bleibt identisch. WICHTIG: alles im Main-Thread halten (UIA ist COM, ggf.
`CoInitialize` pro Thread; am einfachsten über das `autolearn_sig`→Main-Thread).
Permission-Check `AXIsProcessTrusted` entfällt auf Windows (kein TCC) — einfach
direkt versuchen, bei Fehler überspringen + loggen.

**Settings-Checkbox** „Korrekturen automatisch ins Wörterbuch lernen"
(`dict_autolearn`) wie auf macOS in den Allgemein-Block.

---

## 4. Sentry-Sichtbarkeit + Privacy — v2026.6.1, 6.4
**Kopieren:** Funktion `sentry_note(message, level, **extra)` (No-op ohne Sentry).
In `_init_sentry` ergänzen: `include_local_variables=False` und im `before_send`
das Scrubben der Frame-`vars` (schützt API-Key/Text). `sentry_note` an den
Windows-relevanten Fehlerstellen aufrufen (z.B. API-Transkription/Cleanup
fehlgeschlagen). KEINE doppelten Events: `log.error/exception` wird von der
LoggingIntegration schon zum Event — NICHT zusätzlich `capture_exception`.
**Windows lässt die macOS-TCC-Sentry-Meldungen weg** (kein Eingabeüberwachung-Check).

---

## 5. Update lädt direkt die Installdatei — v2026.6.2
`check_for_update` gibt jetzt die **direkte Asset-URL** zurück (statt Release-Seite):
im Treffer das Asset mit `RELEASE_ASSET_SUFFIX` (`.exe`) suchen und dessen
`browser_download_url` liefern (Fallback `html_url`). Button-Text in SettingsView:
„Update herunterladen (.exe)". So muss der User auf GitHub nicht zwischen .exe und
den zwei „Source code"-Links unterscheiden.

---

## 6. Diagnose & Support (PIN-geschützter Versand) — v2026.6.4
**Kopieren:** `DIAGNOSTIC_PIN_SHA256` (= SHA-256 von `25042019`,
`330d473c...bca37`), `_LOG_TEXT_MARKERS`, `_check_diagnostic_pin`,
`_redact_log_text`, `_read_tail`, `send_diagnostic_to_sentry`. App-Methode
`collect_diagnostic()` (Version, OS, Status, sanitisierte Config OHNE API-Keys,
redigiertes Log). SettingsView: `_build_diagnostics_box` + Slots
(`_on_diag_create_clicked`, `_on_diag_send_clicked`, `_on_diag_sent`),
Signal `_diag_sent_sig = Signal(bool,str)`.

**Windows-Abweichung in `collect_diagnostic`:**
- Statt `CGPreflightListenEventAccess`/`AXIsProcessTrusted` (gibt's nicht):
  Windows-relevante Infos rein (OS-Version via `platform.platform()`,
  faster-whisper-Modell, ob globaler Hotkey-Listener läuft).
- `platform.mac_ver()` → `platform.win32_ver()` / `platform.platform()`.
- Log-Pfad: `Path.home()/"IQspeakr.log"` (gleich). Crash-Log ggf. anders/keins.
- „Diagnose erstellen" schreibt nach `Path.home()/"IQspeakr-Diagnose.txt"` (ok).

---

## 7. Cloud-API-Fehler sichtbar machen — v2026.6.4 + 6.5
`_http_error_body(e)` liest den Server-Fehlertext aus HTTPError. In
`_multipart_post` und `cleanup_via_api`: `except urllib.error.HTTPError as e:
raise RuntimeError(f"HTTP {e.code} {e.reason}: {_http_error_body(e)}")`.
`verify_api_key`: bei Nicht-401/403 den Body mitgeben. In `_transcribe_frames`
den Fehlergrund einmalig als Notification zeigen (statt still auf lokal
zurückzufallen).

---

## Build / Release für Windows (am Windows-PC)
1. `__version__` in `windows_v2026.6.0/app.py` auf die gewünschte Version (z.B.
   `2026.6.5`), Version auch in `installer.iss` (`AppVersion`/Output-Name) und
   `build_exe.ps1` (falls dort referenziert) anpassen.
2. `requirements.txt` prüfen: bei UIA-Auto-Lernen `uiautomation` (oder comtypes)
   ergänzen; `sentry-sdk` ist schon drin.
3. `.\build_installer.ps1 -Rebuild` → `dist\IQspeakr-Setup-2026.6.x.exe`.
4. App starten + visuell prüfen: API-Bereich, Auto-Lern-Checkbox, Diagnose-Box,
   Phantom-Filter (kurzer Tap → kein Text), Groq „Key testen" → gültig.
5. **`.exe` ins passende GitHub-Release laden** (Updater filtert je Plattform
   nach Suffix): `gh release upload v2026.6.x "dist\IQspeakr-Setup-2026.6.x.exe"`.
   Hinweis: Releases v2026.6.1–6.5 haben aktuell nur die macOS-.dmg — Windows-
   User sehen erst dann ein Update, wenn ein Release ein `.exe`-Asset trägt.

## Test-Checkliste Windows (Parität zu macOS)
- [ ] Phantom: kurzer/leerer Tastendruck erzeugt KEINEN Text
- [ ] Groq „Key testen" → „API-Key gültig" (User-Agent-Fix wirkt)
- [ ] Mit API: Transkription läuft über Cloud, Fallback bei Fehler sichtbar
- [ ] Cleanup ohne Ollama nutzbar wenn API aktiv; Style schaltet frei
- [ ] Auto-Lernen: Wort korrigieren (ganzes Wort, kurz warten) → „gelernt"
- [ ] Update-Button lädt direkt die .exe
- [ ] Diagnose: „erstellen" zeigt Datei; „senden" mit PIN 25042019 funktioniert,
      diktierter Text im Log ist redigiert, kein API-Key enthalten
