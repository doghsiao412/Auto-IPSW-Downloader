#!/bin/sh
# Installs or updates Auto IPSW Downloader on a Mac as a per-user background service.
# Usage: sh mac/install.sh [address]
#   address: optional IP to serve on (for example this Mac's Tailscale IP). Default: this Mac only.
set -eu
LABEL=com.doghsiao412.auto-ipsw-downloader
SOURCE=$(cd "$(dirname "$0")/.." && pwd)
HOME_DIR="$HOME/Library/Application Support/Auto IPSW Downloader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ADDRESS=${1:-127.0.0.1}
PORT=18766

[ "$(uname)" = Darwin ] || { echo "這個安裝程式只能在 Mac 上執行"; exit 1; }
if xcode-select -p >/dev/null 2>&1 && [ -x /usr/bin/python3 ]; then PYTHON=/usr/bin/python3
else PYTHON=$(command -v python3 || true); fi
[ -n "$PYTHON" ] && "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || {
  echo "找不到 Python 3.9 以上版本。請先在終端機執行：xcode-select --install"; exit 1; }

mkdir -p "$HOME_DIR/app/web" "$HOME_DIR/data" "$HOME/Library/LaunchAgents"
cp "$SOURCE/server.py" "$SOURCE/notifications.py" "$SOURCE/mac_backend.py" "$HOME_DIR/app/"
cp "$SOURCE/web/index.html" "$SOURCE/web/app.js" "$SOURCE/web/style.css" "$HOME_DIR/app/web/"
cp "$SOURCE/mac/setup-mail.sh" "$HOME_DIR/"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PYTHON</string><string>$HOME_DIR/app/server.py</string></array>
  <key>WorkingDirectory</key><string>$HOME_DIR/app</string>
  <key>EnvironmentVariables</key><dict>
    <key>IPSW_BACKEND</key><string>mac</string>
    <key>DATA_DIR</key><string>$HOME_DIR/data</string>
    <key>BIND_ADDRESS</key><string>$ADDRESS</string>
    <key>PORT</key><string>$PORT</string>
    <key>PUBLIC_HOST</key><string>$ADDRESS:$PORT</string>
    <key>PYTHONDONTWRITEBYTECODE</key><string>1</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$HOME_DIR/data/service.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/data/service.log</string>
</dict></plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# Updating: wait for the old service to finish stopping, or the new one fails to register.
n=0; while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && [ $n -lt 40 ]; do sleep 1; n=$((n + 1)); done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安裝並啟動。請用瀏覽器開啟 http://$ADDRESS:$PORT"
echo "要寄信通知，請在這台 Mac 的終端機執行：sh \"$HOME_DIR/setup-mail.sh\""
