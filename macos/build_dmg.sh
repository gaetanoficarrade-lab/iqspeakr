#!/usr/bin/env bash
# ============================================================
#  IQspeakr — DMG Builder
#  Erstellt "IQspeakr Installer.app" und verpackt es in eine DMG
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALLER_SRC="$PROJECT_DIR/installer"
BUILD_DIR="$PROJECT_DIR/build"
DMG_CONTENT="$BUILD_DIR/dmg-content"
INSTALLER_APP="$DMG_CONTENT/IQspeakr Installer.app"
DMG_OUTPUT="$PROJECT_DIR/IQspeakr-Installer.dmg"

echo "▸ Erstelle IQspeakr Installer..."

# Aufräumen
rm -rf "$BUILD_DIR"
mkdir -p "$DMG_CONTENT"

# ============================================================
# Installer.app Bundle erstellen
# ============================================================
mkdir -p "$INSTALLER_APP/Contents/MacOS"
mkdir -p "$INSTALLER_APP/Contents/Resources"

# Info.plist
cat > "$INSTALLER_APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>IQspeakr Installer</string>
    <key>CFBundleDisplayName</key>
    <string>IQspeakr Installer</string>
    <key>CFBundleIdentifier</key>
    <string>com.iqspeakr.installer</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>installer</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSArchitecturePriority</key>
    <array>
        <string>arm64</string>
    </array>
</dict>
</plist>
PLIST

# Executable
cp "$INSTALLER_SRC/gui/installer" "$INSTALLER_APP/Contents/MacOS/installer"
chmod +x "$INSTALLER_APP/Contents/MacOS/installer"

# Resources: Fortschrittsfenster
cp "$INSTALLER_SRC/gui/progress.js" "$INSTALLER_APP/Contents/Resources/"

# Resources: App-Dateien (werden bei Installation nach ~/.iqspeakr kopiert)
cp "$INSTALLER_SRC/app.py" "$INSTALLER_APP/Contents/Resources/"
cp "$INSTALLER_SRC/config.json" "$INSTALLER_APP/Contents/Resources/"
cp "$INSTALLER_SRC/create_icon.py" "$INSTALLER_APP/Contents/Resources/"
cp "$INSTALLER_SRC/requirements.txt" "$INSTALLER_APP/Contents/Resources/"

# Icon (falls vorhanden)
if [ -f "$PROJECT_DIR/IQspeakr.icns" ]; then
    cp "$PROJECT_DIR/IQspeakr.icns" "$INSTALLER_APP/Contents/Resources/AppIcon.icns"
elif [ -f "$PROJECT_DIR/speakr.icns" ]; then
    cp "$PROJECT_DIR/speakr.icns" "$INSTALLER_APP/Contents/Resources/AppIcon.icns"
fi

echo "  ✓ IQspeakr Installer.app erstellt"

# ============================================================
# DMG erstellen
# ============================================================
echo "▸ Erstelle DMG..."

rm -f "$DMG_OUTPUT"

hdiutil create \
    -volname "IQspeakr Installer" \
    -srcfolder "$DMG_CONTENT" \
    -ov \
    -format UDZO \
    "$DMG_OUTPUT"

echo "  ✓ DMG erstellt: $DMG_OUTPUT"

rm -rf "$BUILD_DIR"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  IQspeakr-Installer.dmg ist bereit!          ║"
echo "║                                              ║"
echo "║  Pfad: $DMG_OUTPUT"
echo "╚══════════════════════════════════════════════╝"
