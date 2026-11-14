"""macOS storage backend: verified firmware moves into the iTunes/Finder firmware folders.

Replaces the NAS exporter. Same request/response shape as server.export_call, but runs in-process.
"""
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import ssl
import subprocess
import threading

DATA = Path(os.environ.get('DATA_DIR', str(Path.home() / 'Library/Application Support/Auto IPSW Downloader/data')))
ROOT = Path(os.environ.get('FIRMWARE_ROOT', str(Path.home() / 'Library/iTunes')))
FOLDERS = {'iPhone': 'iPhone Software Updates', 'iPad': 'iPad Software Updates', 'Mac': 'Mac Software Updates'}
KEYCHAIN_SERVICE = 'auto-ipsw-downloader-smtp'
FIELDS = ('sha1', 'sha256', 'size', 'devices', 'version', 'build', 'label')
TITLES = {'new': 'Auto IPSW Downloader：偵測到新版本',
          'complete': 'Auto IPSW Downloader：全部 IPSW 已存入 iTunes 資料夾',
          'test': 'Auto IPSW Downloader：通知測試'}
# The web UI and messages are shared with the NAS edition; wording is adapted when served.
WORDING = (('外接硬碟', 'iTunes 資料夾'),)
PAGE_WORDING = (('NAS 韌體管理', 'Mac 韌體管理'), ('Tailscale 內部服務', '本機服務'), ('最近提交 DSM', '最近送出'),
                ('已排入 DSM 測試通知，請稍後查看收件信箱', '已排入測試通知，稍後會跳出系統通知並寄信'),
                ('沿用 DSM 的寄件與收件設定。', '會跳出 Mac 系統通知，並依寄信設定寄出郵件。'),
                ('電子郵件', '系統通知與郵件'), ("'已連接'", "'可使用'"), ('NAS', 'Mac'))
lock = threading.RLock()


def localize(text, page=False):
    for old, new in WORDING + (PAGE_WORDING if page else ()):
        text = text.replace(old, new)
    return text


def valid_name(name):
    return isinstance(name, str) and bool(re.fullmatch(r'[A-Za-z0-9_.,+\-]{1,220}\.ipsw', name))


def folder(request):
    kind = str(request.get('label', '')).split(' ')[0]
    if kind not in FOLDERS:
        raise ValueError('無法判斷韌體的裝置類別')
    path = ROOT / FOLDERS[kind]
    if ROOT.is_symlink() or path.is_symlink():
        raise ValueError('目的地不可為符號連結')
    return path


def load():
    path = DATA / 'managed.json'
    manifest = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(manifest, dict) or any(not valid_name(n) for n in manifest):
        raise ValueError('韌體管理紀錄格式錯誤')
    return manifest


def save(manifest):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / 'managed.tmp'
    with open(tmp, 'w') as output:
        json.dump(manifest, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    tmp.replace(DATA / 'managed.json')


def hashes(path):
    h1, h256 = hashlib.sha1(), hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            h1.update(chunk)
            h256.update(chunk)
    return h1.hexdigest(), h256.hexdigest()


def status(request):
    ROOT.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(ROOT)
    return {'connected': True, 'free': usage.free, 'total': usage.total, 'folder': str(ROOT)}


def has(request):
    name = request['name']
    if not valid_name(name):
        raise ValueError('不合法的檔名')
    path = folder(request) / name
    # Moving within one volume needs no extra room; the staging check already covers the download.
    result = {'present': False, 'available': 2 ** 62}
    if path.is_symlink() or not path.is_file() or path.stat().st_size != request['size']:
        return result
    manifest = load()
    item = manifest.get(name, {})
    if not (item.get('verified') is True and item.get('sha1') == request['sha1']):
        # A file Finder or the user already put here: adopt it only when it is exactly Apple's file.
        sha1, sha256 = hashes(path)
        if sha1 != request['sha1']:
            return result
        item = {**{k: request[k] for k in FIELDS if k in request}, 'sha256': sha256, 'verified': True}
        manifest[name] = item
        save(manifest)
    return {'present': True, 'available': 2 ** 62, 'sha256': item['sha256'], 'mtime': path.stat().st_mtime}


def export(request):
    name = request['name']
    if not valid_name(name):
        raise ValueError('不合法的管理檔名')
    source = DATA / 'staging' / name
    dest = folder(request)
    target = dest / name
    if target.is_symlink() or source.is_symlink():
        raise ValueError('檔案不可為符號連結')
    manifest = load()
    if target.exists() and name not in manifest:
        raise ValueError('iTunes 資料夾已有非本工具管理的同名檔案，不覆蓋')
    if not source.is_file() or source.stat().st_size != request['size']:
        raise ValueError('內部暫存不存在或大小不符')
    dest.mkdir(parents=True, exist_ok=True)
    # Journal ownership first, so a crash between the move and the final save stays recoverable.
    manifest[name] = {k: request[k] for k in FIELDS}
    save(manifest)
    if os.stat(source).st_dev == os.stat(dest).st_dev:
        os.replace(source, target)  # Same volume: instant, and the hash checked in staging still holds.
    else:
        partial = dest / (name + '.partial')
        shutil.copyfile(source, partial)
        if hashes(partial)[1] != request['sha256']:
            partial.unlink()
            raise ValueError('搬移後的檔案驗證失敗，保留內部暫存')
        os.replace(partial, target)
    manifest[name]['verified'] = True
    save(manifest)
    return {'exported': True, 'folder': str(dest)}


def prune(request):
    keep, covered = set(request['keep']), set(request['covered'])
    manifest = load()
    removed = []
    for name, item in list(manifest.items()):
        if name in keep or not set(item['devices']).issubset(covered):
            continue
        old = folder(item) / name
        if old.is_symlink():
            continue
        if old.is_file():
            if old.stat().st_size != item['size']:
                continue  # Not the file this tool placed; leave it alone.
            old.unlink()
        removed.append(name)
        del manifest[name]
    save(manifest)
    return {'removed': removed}


def banner(title, text):
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    subprocess.run(['/usr/bin/osascript', '-e', script, title, text], capture_output=True, timeout=20)


def mail_settings():
    path = DATA / 'mail.json'
    if not path.is_file():
        return None
    settings = json.loads(path.read_text())
    if (not all(isinstance(settings.get(k), str) and settings[k] for k in ('host', 'username', 'to'))
            or type(settings.get('port', 587)) is not int):
        raise ValueError('mail.json 格式錯誤')
    return settings


def mail_password(username):
    result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', KEYCHAIN_SERVICE,
                             '-a', username, '-w'], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise ValueError('鑰匙圈找不到寄信密碼，請重新執行 setup-mail.sh')
    return result.stdout.rstrip('\n')


def send_mail(settings, title, details):
    message = EmailMessage()
    message['Subject'] = title
    message['From'] = settings.get('from') or settings['username']
    message['To'] = settings['to']
    message.set_content(details)
    port = settings.get('port', 587)
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(settings['host'], port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(settings['host'], port, timeout=30)
    with server:
        if port != 465:
            server.starttls(context=context)
        server.login(settings['username'], mail_password(settings['username']))
        server.send_message(message)


def notify(request):
    kind, event_id, details = request.get('kind'), request.get('event_id'), request.get('details')
    if (kind not in TITLES or not isinstance(event_id, str) or not re.fullmatch(r'[0-9a-f]{64}', event_id)
            or not isinstance(details, str) or len(details) > 8000 or '\0' in details):
        raise ValueError('通知格式錯誤')
    details = localize(details)
    ledger = DATA / 'notified.json'
    done = json.loads(ledger.read_text()) if ledger.exists() else {}
    state = done.setdefault(event_id, {})
    if not state.get('banner'):
        banner(TITLES[kind], details.split('\n\n')[0][:200])
        state['banner'] = True
    settings = mail_settings()
    try:
        if settings and not state.get('mail'):
            send_mail(settings, TITLES[kind], details)
            state['mail'] = True
    finally:
        # The banner is remembered even when mail fails, so a mail retry never repeats it.
        tmp = ledger.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(list(done.items())[-200:])))
        tmp.replace(ledger)
    return {'submitted': True, 'mail': bool(settings)}


ACTIONS = {'status': status, 'has': has, 'export': export, 'prune': prune, 'notify': notify}


def call(request):
    action = request.get('action')
    if action in ('eject', 'mount'):
        raise ValueError('Mac 版直接存入內建磁碟，不需要退出或掛載')
    if action not in ACTIONS:
        raise ValueError('未知操作')
    try:
        if action == 'notify':
            return ACTIONS[action](request)  # Mail can be slow; it must not hold up file moves.
        with lock:
            return ACTIONS[action](request)
    except (OSError, smtplib.SMTPException) as e:
        raise ValueError('%s' % e)


def keep_awake(service):
    """Hold off idle sleep while a batch runs. Closing the lid still sleeps; downloads resume on wake."""
    import time
    holder = None
    while True:
        with service.lock:
            active = service.batch_active
        if active and holder is None:
            holder = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())])
        elif not active and holder is not None:
            holder.terminate()
            holder = None
        time.sleep(5)
