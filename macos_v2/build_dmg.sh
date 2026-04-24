#!/usr/bin/env bash
# ============================================================
#  IQspeakr macOS v2 — Build Script
#  Baut .app via PyInstaller, setzt LSUIElement + packt in DMG
# ============================================================
#
# Voraussetzungen:
#   - Python 3.11+ (Apple-Silicon nativ fuer MPS-Support)
#   - Homebrew + ffmpeg (`brew install ffmpeg`)
#   - Internet fuer pip + Whisper-Modell-Download beim ersten Start
#
# Ausfuehren (im macos_v2-Ordner):
#   ./build_dmg.sh
#
# Ergebnis:
#   dist/IQspeakr.app        — fertige App
#   IQspeakr-v2.dmg          — verteilbares DMG

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "[1/5] Virtuelle Umgebung anlegen..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

echo "[2/5] Dependencies installieren..."
pip install --upgrade pip --quiet
pip install --quiet -r requirements.txt
pip install --quiet pyinstaller

echo "[3/5] PyInstaller: .app bauen..."
rm -rf dist build
pyinstaller \
    --name IQspeakr \
    --windowed \
    --onedir \
    --noconfirm \
    --clean \
    --osx-bundle-identifier com.iqspeakr.app \
    --icon IQspeakr.icns \
    --collect-all whisper \
    --collect-submodules pynput \
    --collect-submodules sounddevice \
    --exclude-module tkinter \
    --add-data "config.json:." \
    app.py

APP_BUNDLE="dist/IQspeakr.app"
PLIST="$APP_BUNDLE/Contents/Info.plist"

echo "[4/5] Info.plist patchen (LSUIElement + Mikro-Permission)..."
# LSUIElement=YES → kein Dock-Icon, nur Menuebalken-Icon
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST"

# Mikrofon-Berechtigungs-Prompt-Text fuer macOS
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string 'IQspeakr braucht Zugriff auf das Mikrofon fuer Sprache-zu-Text.'" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Set :NSMicrophoneUsageDescription 'IQspeakr braucht Zugriff auf das Mikrofon fuer Sprache-zu-Text.'" "$PLIST"

echo "[5/5] DMG packen..."
DMG_OUTPUT="$PROJECT_DIR/IQspeakr-v2.dmg"
rm -f "$DMG_OUTPUT"
DMG_STAGE="$(mktemp -d)/IQspeakr"
mkdir -p "$DMG_STAGE"
cp -R "$APP_BUNDLE" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"

hdiutil create \
    -volname "IQspeakr v2" \
    -srcfolder "$DMG_STAGE" \
    -ov \
    -format UDZO \
    "$DMG_OUTPUT" > /dev/null

rm -rf "$DMG_STAGE"

echo ""
echo "============================================"
echo "  IQspeakr.app + DMG sind bereit:"
echo "  $APP_BUNDLE"
echo "  $DMG_OUTPUT"
echo "============================================"
echo ""
echo "Hinweis: Beim ersten Start muss die App in"
echo "  Systemeinstellungen → Datenschutz & Sicherheit"
echo "  → Bedienungshilfen + Input Monitoring + Mikrofon"
echo "freigegeben werden."
