#!/bin/sh
# Stops and removes the background service. Downloaded firmware, settings and records are kept.
set -eu
LABEL=com.doghsiao412.auto-ipsw-downloader
HOME_DIR="$HOME/Library/Application Support/Auto IPSW Downloader"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME_DIR/app" "$HOME_DIR/setup-mail.sh"
echo "已移除背景服務。韌體、設定與紀錄都保留。"
