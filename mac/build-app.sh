#!/bin/sh
# Builds dist/Auto IPSW Downloader.app and a disk image of it. Usage: sh mac/build-app.sh
set -eu
SOURCE=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -n "s/^VERSION = '\(.*\)'/\1/p" "$SOURCE/server.py")
APP="$SOURCE/dist/Auto IPSW Downloader.app"
rm -rf "$SOURCE/dist"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/web" "$APP/Contents/Resources/mac"
cp "$SOURCE/server.py" "$SOURCE/notifications.py" "$SOURCE/mac_backend.py" "$APP/Contents/Resources/"
cp "$SOURCE/web/index.html" "$SOURCE/web/app.js" "$SOURCE/web/style.css" "$APP/Contents/Resources/web/"
cp "$SOURCE/mac/install.sh" "$SOURCE/mac/uninstall.sh" "$SOURCE/mac/setup-mail.sh" "$APP/Contents/Resources/mac/"
BUILD=$(mktemp -d)
xcrun swiftc -O -swift-version 5 -parse-as-library -target arm64-apple-macos13.0 \
  "$SOURCE/mac/App.swift" -o "$APP/Contents/MacOS/AutoIPSWDownloader"
xcrun swift "$SOURCE/mac/make-icon.swift" "$BUILD/icon.png"
mkdir "$BUILD/AppIcon.iconset"
for SIZE in 16 32 128 256 512; do
  sips -z $SIZE $SIZE "$BUILD/icon.png" --out "$BUILD/AppIcon.iconset/icon_${SIZE}x${SIZE}.png" >/dev/null
  sips -z $((SIZE * 2)) $((SIZE * 2)) "$BUILD/icon.png" --out "$BUILD/AppIcon.iconset/icon_${SIZE}x${SIZE}@2x.png" >/dev/null
done
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
cp "$BUILD/icon.png" "$SOURCE/dist/icon.png"
rm -rf "$BUILD"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Auto IPSW Downloader</string>
  <key>CFBundleIdentifier</key><string>com.doghsiao412.auto-ipsw-downloader.app</string>
  <key>CFBundleExecutable</key><string>AutoIPSWDownloader</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$(date +%Y%m%d%H%M%S)</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
xattr -cr "$APP"
codesign --force --sign - "$APP"
DMG="$SOURCE/dist/Auto-IPSW-Downloader-Mac-$VERSION.dmg"
STAGE=$(mktemp -d)
ditto "$APP" "$STAGE/Auto IPSW Downloader.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "Auto IPSW Downloader" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "$DMG"
