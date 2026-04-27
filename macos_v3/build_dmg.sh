#!/usr/bin/env bash
# ============================================================
#  IQspeakr v3 — DMG Builder
#  Erstellt "IQspeakr Installer.app" und verpackt es in eine DMG
#
#  Pattern identisch zu v1 (macos/build_dmg.sh):
#  Kleiner Bash-Installer statt PyInstaller-Bundle.
#  Python-Deps werden erst beim Install via pip nachgeladen.
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALLER_SRC="$PROJECT_DIR/installer"
BUILD_DIR="$PROJECT_DIR/build"
DMG_CONTENT="$BUILD_DIR/dmg-content"
INSTALLER_APP="$DMG_CONTENT/IQspeakr Installer.app"
DMG_OUTPUT="$PROJECT_DIR/IQspeakr-v3-Installer.dmg"

echo "▸ Erstelle IQspeakr v3 Installer..."

# Aufräumen — aber Caches (ffmpeg, snapshot) NICHT loeschen, sonst muessen
# wir bei jedem Build 2 GB neu tar'en und 50 MB ffmpeg neu downloaden.
rm -rf "$DMG_CONTENT"
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
    <string>3.0</string>
    <key>CFBundleShortVersionString</key>
    <string>3.0</string>
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

# Resources: v3-App-Dateien (werden bei Installation nach ~/.iqspeakr kopiert)
# Abweichung zu v1: Quellen liegen direkt im macos_v3/-Root, nicht in installer/
cp "$PROJECT_DIR/app.py" "$INSTALLER_APP/Contents/Resources/"
cp "$PROJECT_DIR/tray_proc.py" "$INSTALLER_APP/Contents/Resources/"
cp "$PROJECT_DIR/config.json" "$INSTALLER_APP/Contents/Resources/"
cp "$PROJECT_DIR/requirements.txt" "$INSTALLER_APP/Contents/Resources/"

# ffmpeg + venv-Pakete werden zur Install-Zeit downloaded (V1-Pattern).
# Das haelt das DMG klein (<1 MB) und macht Updates einfach — der Installer
# zieht aktuelle Versionen.

# C-Launcher kompilieren (echtes Mach-O-Binary statt Bash-Wrapper).
# Mach-O ist Pflicht, damit macOS Tahoe TCC die App-Identitaet stabil
# auf das Bundle (com.iqspeakr.app) mappt. Bash-Skripte als Bundle-
# Hauptbinary fuehren bei Tahoe zu Identity-Mapping auf den exec'd
# Python-Subprocess — und damit zu unzuverlaessigen TCC-Permissions.
LAUNCHER_BIN="$PROJECT_DIR/build/launcher"
mkdir -p "$PROJECT_DIR/build"
echo "  ▸ Kompiliere C-Launcher (arm64)..."
clang -O2 -arch arm64 -o "$LAUNCHER_BIN" "$PROJECT_DIR/launcher.c"
cp "$LAUNCHER_BIN" "$INSTALLER_APP/Contents/Resources/launcher"
chmod +x "$INSTALLER_APP/Contents/Resources/launcher"
echo "  ✓ Launcher kompiliert + ins DMG kopiert ($(file "$INSTALLER_APP/Contents/Resources/launcher" | awk -F: '{print $2}'))"

# Icon: v3 liefert IQspeakr.icns mit — wird sowohl fuer Installer als auch
# fuer die spaeter installierte App verwendet.
if [ -f "$PROJECT_DIR/IQspeakr.icns" ]; then
    cp "$PROJECT_DIR/IQspeakr.icns" "$INSTALLER_APP/Contents/Resources/AppIcon.icns"
    cp "$PROJECT_DIR/IQspeakr.icns" "$INSTALLER_APP/Contents/Resources/IQspeakr.icns"
fi

echo "  ✓ IQspeakr Installer.app erstellt"

# ============================================================
# DMG erstellen
# ============================================================
echo "▸ Erstelle DMG..."

rm -f "$DMG_OUTPUT"

hdiutil create \
    -volname "IQspeakr v3 Installer" \
    -srcfolder "$DMG_CONTENT" \
    -ov \
    -format UDZO \
    "$DMG_OUTPUT"

echo "  ✓ DMG erstellt: $DMG_OUTPUT"

# Nur dmg-content aufraeumen — ffmpeg-cache und snapshot bleiben fuer
# schnelle Re-Builds (sonst muessen wir bei jedem Build 2 GB neu tar'en)
rm -rf "$DMG_CONTENT"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  IQspeakr-v3-Installer.dmg ist bereit!       ║"
echo "║                                              ║"
echo "║  Pfad: $DMG_OUTPUT"
echo "╚══════════════════════════════════════════════╝"
