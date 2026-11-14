#!/bin/sh
# Run once via interactive sudo. Never changes sudoers or SSH daemon configuration.
set -eu
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
test "$(id -u)" = 0 || { echo '請用 sudo 執行'; exit 1; }
src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname "$src")  # shared engine and web page live one level up
base=/volume1/docker/ipsw-downloader
# Site-specific values live in deploy.env (not in the repository); see deploy.env.example.
test -f "$src/deploy.env" || { echo '找不到 deploy.env，請先複製 deploy.env.example 並填入設定'; exit 1; }
. "$src/deploy.env"
: "${NAS_USER:?deploy.env 缺少 NAS_USER}" "${PUBLIC_HOST:?deploy.env 缺少 PUBLIC_HOST}"
mount=/volumeUSB1/usbshare
python3 -c 'import os; assert os.path.ismount("/volumeUSB1/usbshare"), "外接硬碟未掛載，停止安裝"'
docker info --format 'Docker {{.ServerVersion}} / {{.Driver}}'
mkdir -p "$base/web" "$base/data/staging" "$base/ssh"
cp "$root/server.py" "$root/notifications.py" "$src/exporter.py" "$src/dsm-mail-report.py" "$src/Dockerfile" "$src/compose.yaml" "$src/.dockerignore" "$base/"
cp "$root/web/index.html" "$root/web/app.js" "$root/web/style.css" "$base/web/"
printf 'NAS_USER=%s\nPUBLIC_HOST=%s\n' "$NAS_USER" "$PUBLIC_HOST" > "$base/.env"
chmod 644 "$base/.env"
chmod 755 "$base"
chown -R 1026:100 "$base/data" "$base/ssh"
chmod 700 "$base/data" "$base/data/staging"
chmod 700 "$base/ssh"
if [ ! -e "$base/disk-id" ]; then
    python3 -c 'import secrets; print(secrets.token_hex(24))' > "$base/disk-id"
fi
if [ -e "$mount/.ipsw-downloader-id" ]; then
    cmp "$base/disk-id" "$mount/.ipsw-downloader-id" || { echo '外接硬碟識別不符，停止'; exit 1; }
else
    cp "$base/disk-id" "$mount/.ipsw-downloader-id"
fi
chmod 644 "$base/disk-id" "$base/exporter.py" "$base/dsm-mail-report.py" "$base/server.py" "$base/notifications.py"
if [ ! -e "$base/ssh/id_ed25519" ]; then
    ssh-keygen -q -t ed25519 -N '' -f "$base/ssh/id_ed25519" -C ipsw-local-export
fi
chown -R 1026:100 "$base/ssh"
chmod 600 "$base/ssh/id_ed25519"
test -f /etc/ssh/ssh_host_ed25519_key.pub || { echo '找不到 NAS SSH 公開主機金鑰'; exit 1; }
awk '{print "127.0.0.1 " $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub > "$base/ssh/known_hosts"
chown 1026:100 "$base/ssh/known_hosts"
home=/var/services/homes/$NAS_USER
test -d "$home" || { echo '找不到使用者家目錄'; exit 1; }
mkdir -p "$home/.ssh"
touch "$home/.ssh/authorized_keys"
key=$(cat "$base/ssh/id_ed25519.pub")
if ! grep -Fq "$key" "$home/.ssh/authorized_keys"; then
    printf 'from="127.0.0.1",restrict,command="/bin/python3 /volume1/docker/ipsw-downloader/exporter.py" %s\n' "$key" >> "$home/.ssh/authorized_keys"
fi
chown 1026:100 "$home/.ssh" "$home/.ssh/authorized_keys"
chmod 700 "$home/.ssh"
chmod 600 "$home/.ssh/authorized_keys"
cd "$base"
docker compose config --quiet
docker compose build
# Remove before creating so DSM does not retain Compose's temporary renamed container.
docker compose stop -t 30 ipsw
docker compose rm -f ipsw
docker compose up -d --no-build ipsw
docker compose ps
echo "部署完成：http://$PUBLIC_HOST （保留既有設定；首次安裝預設暫停）"
