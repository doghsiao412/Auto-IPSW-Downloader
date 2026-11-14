"""Forced-command SSH endpoint. No arbitrary paths, URLs, or shell commands accepted."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

BASE = Path('/volume1/docker/ipsw-downloader')
DEST_NAME = 'IPSW-Auto'


def valid_name(name):
    return isinstance(name, str) and bool(re.fullmatch(r'[A-Za-z0-9_.,+\-]{1,220}\.ipsw', name))


def digest(path, progress=None, guard=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        count = 0
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            if guard:
                guard()
            h.update(chunk)
            count += len(chunk)
            if progress:
                progress(count)
    return h.hexdigest()


class ExportProgress:
    def __init__(self, request):
        self.name = request['name']
        self.total = request['size']
        if not valid_name(self.name) or type(self.total) is not int or self.total <= 0:
            raise ValueError('複製進度格式錯誤')
        self.stage = None
        self.written = 0
        self.update('waiting', 0)

    def update(self, stage, count, force=False):
        now = time.monotonic()
        if stage != self.stage:
            self.stage = stage
            self.started = now
            self.origin = count
            force = True
        if not force and now - self.written < 0.5:
            return
        elapsed = now - self.started
        speed = max(0, count - self.origin) / elapsed if elapsed >= 1 else 0
        data = {'name': self.name, 'stage': stage, 'progress': count, 'total': self.total,
                'speed_bps': speed, 'eta_seconds': (self.total - count) / speed if speed and stage in ('copying', 'readback') else None,
                'updated_at': time.time(), 'active': stage not in ('done', 'error')}
        path = BASE / 'data' / 'export-progress.json'
        tmp = path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
        tmp.replace(path)
        self.written = now


def mounted_disks():
    for line in Path('/proc/mounts').read_text().splitlines():
        fields = line.split()
        path = fields[1].replace('\\040', ' ')
        if path.startswith('/volumeUSB') and '/usbshare' in path and os.path.ismount(path):
            yield Path(path)


def find_disk():
    expected = (BASE / 'disk-id').read_text().strip()
    for mount in mounted_disks():
        marker = mount / '.ipsw-downloader-id'
        if not marker.is_symlink() and marker.is_file() and marker.read_text().strip() == expected:
            return mount
    raise ValueError('外接硬碟未連接，或尚未掛載指定硬碟；保留內部暫存等待重試')


def usb_device(mount):
    # DSM names external USB disks usb1, usb2…; the partition is /dev/usb1p1.
    for line in Path('/proc/mounts').read_text().splitlines():
        fields = line.split()
        if fields[1].replace('\\040', ' ') == str(mount):
            match = re.fullmatch(r'/dev/(usb\d+)(?:p\d+)?', fields[0])
            if match:
                return match[1]
    raise ValueError('找不到外接硬碟的 USB 裝置名稱，請改用 DSM 退出')


def eject(request):
    fd = os.open(BASE / 'data' / 'export.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            # Holding the export lock keeps copies, readbacks and cleanup off the disk while unmounting.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('外接硬碟正在複製或驗證，請等完成後再退出')
        mount = find_disk()
        device = usb_device(mount)
        os.sync()
        result = subprocess.run(['sudo', '-n', '/usr/syno/bin/synousbdisk', '-umount', device],
                                capture_output=True, text=True, timeout=120)
        if os.path.ismount(mount):
            raise ValueError('退出失敗，硬碟可能仍被其他程式使用；請改用 DSM 退出（%s）'
                             % (result.stderr or result.stdout).strip()[-200:])
        remember = BASE / 'data' / 'ejected-device'
        if not remember.is_symlink():
            remember.write_text(device)
        return {'ejected': True, 'device': device}


def mounted_sources():
    return {line.split()[0] for line in Path('/proc/mounts').read_text().splitlines()}


def usb_devices():
    return sorted(p.name for p in Path('/dev').glob('usb*') if re.fullmatch(r'usb\d+', p.name))


def mount_disk(request):
    try:
        return {'mounted': True, 'already': True, 'folder': str(find_disk() / DEST_NAME)}
    except ValueError:
        pass
    remember = BASE / 'data' / 'ejected-device'
    remembered = remember.read_text().strip() if remember.is_file() and not remember.is_symlink() else ''
    mounted = mounted_sources()
    # Prefer the disk this tool ejected; otherwise try unmounted USB disks, as DSM does on plug-in.
    others = usb_devices()
    devices = ([remembered] if re.fullmatch(r'usb\d+', remembered) else []) + [d for d in others if d != remembered]
    for device in devices:
        if any(m == '/dev/' + device or m.startswith('/dev/%sp' % device) for m in mounted):
            continue
        subprocess.run(['sudo', '-n', '/usr/syno/bin/synousbdisk', '-mount', device],
                       capture_output=True, text=True, timeout=120)
        try:
            mount = find_disk()
        except ValueError:
            continue
        remember.unlink(missing_ok=True)
        return {'mounted': True, 'device': device, 'folder': str(mount / DEST_NAME)}
    raise ValueError('找不到可重新掛載的指定硬碟；若已拔除，請重新插上')


def run(request):
    if request.get('action') == 'notify':
        return notify(request)
    if request.get('action') == 'eject':
        return eject(request)
    if request.get('action') == 'mount':
        return mount_disk(request)
    if request.get('action') in ('status', 'has'):
        # Read-only: must not wait behind a running copy, or new downloads could not start meanwhile.
        return operate(request)
    # This lock lives on the host and survives container replacement and disconnected SSH clients.
    fd = os.open(BASE / 'data' / 'export.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if request.get('action') != 'export':
            return operate(request)
        progress = ExportProgress(request)
        try:
            result = operate(request, progress)
            progress.update('done', request['size'], force=True)
            return result
        except Exception:
            progress.update('error', 0, force=True)
            raise


def notify(request):
    titles = {'new': 'Auto IPSW Downloader：偵測到新版本',
              'complete': 'Auto IPSW Downloader：全部 IPSW 已同步至外接硬碟',
              'test': 'Auto IPSW Downloader：DSM 郵件通知測試'}
    kind, event_id, details = request.get('kind'), request.get('event_id'), request.get('details')
    if (kind not in titles or not isinstance(event_id, str) or not re.fullmatch(r'[0-9a-f]{64}', event_id)
        or not isinstance(details, str) or len(details) > 8000 or '\0' in details):
        raise ValueError('通知格式錯誤')
    task_config = BASE / 'dsm-task.json'
    if not task_config.is_file():
        raise ValueError('DSM 郵件工作尚未設定')
    task_id = json.loads(task_config.read_text())['id']
    if type(task_id) is not int or task_id <= 0:
        raise ValueError('DSM 工作識別碼錯誤')
    data = BASE / 'data'
    fd = os.open(data / 'dsm-mail.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ack = data / 'dsm-mail-ack.json'
        ledger = data / 'dsm-mail-submitted.json'
        submitted = json.loads(ledger.read_text()) if ledger.exists() else []
        acknowledged = lambda: ack.is_file() and json.loads(ack.read_text()).get('event_id') == event_id
        if event_id in submitted:
            return {'submitted': True, 'duplicate': True}
        if not acknowledged():
            pending = data / 'dsm-mail-request.json'
            tmp = pending.with_suffix('.tmp')
            out_fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out_fd, 'w') as output:
                json.dump({'event_id': event_id, 'title': titles[kind], 'details': details}, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            tmp.replace(pending)
            result = subprocess.run(['sudo', '-n', '/usr/syno/bin/synoschedtask', '--run',
                'id=%d' % task_id, 'check_time=0', 'check_status=0'], capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise ValueError('無法觸發 DSM 郵件工作')
            deadline = time.monotonic() + 45
            while not acknowledged() and time.monotonic() < deadline:
                time.sleep(0.25)
            if not acknowledged():
                raise ValueError('DSM 郵件工作尚未完成，將稍後重試')
        submitted.append(event_id)
        tmp = ledger.with_suffix('.tmp')
        tmp.write_text(json.dumps(submitted))
        tmp.replace(ledger)
        return {'submitted': True}
def operate(request, progress=None):
    mount = find_disk()
    # Pin the actual filesystem: even a detach between checks cannot write into the NAS root disk.
    fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        device = os.fstat(fd).st_dev
        root = Path('/proc/self/fd') / str(fd)
        def guard():
            if not os.path.ismount(mount) or os.stat(mount).st_dev != device:
                raise ValueError('外接硬碟已拔除，已保留內部暫存')
        guard()
        usage = shutil.disk_usage(root)
        free = usage.free
        if request['action'] == 'status':
            return {'connected': True, 'free': free, 'total': usage.total, 'folder': str(mount / DEST_NAME)}
        dest = root / DEST_NAME
        if dest.is_symlink():
            raise ValueError('目的地不可為符號連結')
        if request['action'] == 'has' and not dest.exists():
            return {'present': False, 'available': free}
        dest.mkdir(exist_ok=True)
        manifest_path = dest / '.managed.json'
        if manifest_path.is_symlink():
            raise ValueError('下載紀錄不可為符號連結')
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        if not isinstance(manifest, dict) or any(not valid_name(n) for n in manifest):
            raise ValueError('外接硬碟管理紀錄格式錯誤')
        def save():
            guard()
            tmp = dest / '.managed.tmp'
            if tmp.is_symlink():
                raise ValueError('不安全的紀錄路徑')
            with open(tmp, 'w') as output:
                json.dump(manifest, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            tmp.replace(manifest_path)
        if request['action'] == 'prune':
            keep = set(request['keep'])
            covered = set(request['covered'])
            removed = []
            for name, item in list(manifest.items()):
                if name in keep or not set(item['devices']).issubset(covered):
                    continue
                old = dest / name
                guard()
                if old.exists():
                    if old.is_symlink() or digest(old) != item['sha256']:
                        continue
                    guard()
                    old.unlink()
                partial = dest / (name + '.partial')
                if item.get('partial_owned') and partial.is_file() and not partial.is_symlink():
                    guard()
                    partial.unlink()
                removed.append(name)
                del manifest[name]
            save()
            return {'removed': removed}
        if request['action'] == 'has':
            name = request['name']
            if not valid_name(name):
                raise ValueError('不合法的檔名')
            path = dest / name
            item = manifest.get(name, {})
            partial = dest / (name + '.partial')
            reusable = partial.stat().st_size if partial.is_file() and not partial.is_symlink() else 0
            present = bool(not path.is_symlink() and path.is_file()
                and path.stat().st_size == request['size'] and item.get('verified') is True
                and item.get('sha256') == request['sha256'])
            result = {'present': present, 'available': free + reusable}
            if present:
                result['mtime'] = path.stat().st_mtime  # When the copy landed; lets the app show the real sync time.
            return result
        if request['action'] != 'export':
            raise ValueError('未知操作')
        name = request['name']
        if not valid_name(name):
            raise ValueError('不合法的管理檔名')
        source = BASE / 'data' / 'staging' / name
        target = dest / name
        expected = request['sha256']
        size = request['size']
        if target.is_symlink() or source.is_symlink():
            raise ValueError('檔案不可為符號連結')
        if target.exists():
            if name not in manifest:
                raise ValueError('目的地已有非本工具管理的同名檔案，不覆蓋')
            if progress:
                progress.update('readback', 0)
            if target.stat().st_size == size and digest(target,
                    (lambda n: progress.update('readback', n)) if progress else None, guard) == expected:
                manifest[name].update({k: request[k] for k in ('sha256', 'size', 'devices', 'version', 'build', 'label')})
                manifest[name]['verified'] = True
                save()
                return {'exported': True, 'existing': True, 'folder': str(mount / DEST_NAME)}
        if not source.is_file() or source.stat().st_size != size:
            raise ValueError('內部暫存不存在或大小不符')
        partial = dest / (name + '.partial')
        if partial.is_symlink():
            raise ValueError('暫存不可為符號連結')
        reusable = partial.stat().st_size if partial.is_file() else 0
        if free + reusable < size + 2 * 1024 ** 3:
            raise ValueError('外接硬碟空間不足；保留舊版及內部暫存，不會先刪舊版')
        if partial.exists() and name not in manifest:
            raise ValueError('目的地已有未受管理的同名暫存檔，不覆蓋')
        manifest[name] = {k: request[k] for k in ('sha256', 'size', 'devices', 'version', 'build', 'label')}
        manifest[name]['partial_owned'] = True
        save()
        h = hashlib.sha256()
        copied = 0
        if progress:
            progress.update('copying', 0)
        with open(source, 'rb') as src, open(partial, 'wb') as dst:
            while True:
                guard()
                chunk = src.read(4 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                h.update(chunk)
                copied += len(chunk)
                if progress:
                    progress.update('copying', copied)
            if progress:
                progress.update('flushing', copied, force=True)
            dst.flush()
            os.fsync(dst.fileno())
        guard()
        if progress:
            progress.update('readback', 0)
        if h.hexdigest() != expected or digest(partial,
                (lambda n: progress.update('readback', n)) if progress else None, guard) != expected:
            raise ValueError('外接硬碟檔案驗證失敗，保留內部暫存')
        # Journal ownership before rename so a disconnect after rename remains recoverable.
        manifest[name] = {k: request[k] for k in ('sha256', 'size', 'devices', 'version', 'build', 'label')}
        save()
        guard()
        partial.replace(target)
        manifest[name]['verified'] = True
        save()
        return {'exported': True, 'folder': str(mount / DEST_NAME)}
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.buffer.read(16385))
        result = run(request)
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
