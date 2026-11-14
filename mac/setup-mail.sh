#!/bin/sh
# Sets up mail notifications. Run this yourself on the Mac: the password goes straight into your Keychain.
set -eu
DATA="$HOME/Library/Application Support/Auto IPSW Downloader/data"
SERVICE=auto-ipsw-downloader-smtp
mkdir -p "$DATA"
printf '寄信伺服器（Gmail 請直接按 Enter）[smtp.gmail.com]：'; read -r HOST; HOST=${HOST:-smtp.gmail.com}
printf '連接埠 [587]：'; read -r PORT; PORT=${PORT:-587}
printf '寄件帳號（完整信箱）：'; read -r USERNAME
printf '收件信箱（按 Enter 表示寄給自己）：'; read -r TO; TO=${TO:-$USERNAME}
case "$PORT" in ''|*[!0-9]*) echo "連接埠必須是數字"; exit 1;; esac
[ -n "$USERNAME" ] || { echo "寄件帳號不可空白"; exit 1; }
echo "接下來請輸入寄信密碼（Gmail 要用「應用程式密碼」，不是登入密碼）。輸入時畫面不會顯示。"
security add-generic-password -U -s "$SERVICE" -a "$USERNAME" -T /usr/bin/security -w
umask 077
PYTHON=/usr/bin/python3; xcode-select -p >/dev/null 2>&1 || PYTHON=$(command -v python3)
HOST="$HOST" PORT="$PORT" USERNAME="$USERNAME" TO="$TO" "$PYTHON" - "$DATA/mail.json" <<'PY'
import json, os, sys
json.dump({'host': os.environ['HOST'], 'port': int(os.environ['PORT']),
           'username': os.environ['USERNAME'], 'to': os.environ['TO']}, open(sys.argv[1], 'w'), ensure_ascii=False)
PY
echo "設定完成。請到網頁「通知與紀錄」按「寄送測試通知」確認收得到信。"
