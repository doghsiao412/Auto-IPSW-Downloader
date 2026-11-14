"""Dependency-free IPSW scheduler and local web UI. Downloads only to internal staging."""
import copy
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from notifications import Notifications

VERSION = '1.5'
API = 'https://api.ipsw.me/v4'
SYSTEMS = {'iPhone': 'iOS', 'iPad': 'iPadOS', 'Mac': 'macOS'}
MAC = os.environ.get('IPSW_BACKEND') == 'mac'  # macOS edition: files go to the iTunes firmware folders
if MAC:
    import mac_backend
DATA = mac_backend.DATA if MAC else Path(os.environ.get('DATA_DIR', '/data'))
WEB = Path(__file__).parent / 'web'
MAX_TRANSFERS = 5
MAX_BACKLOG = 10  # verified files allowed to queue for the external disk
SLOW_WINDOW = 120  # seconds of below-threshold speed before reconnecting
MAX_NETWORK_RETRIES = 5
INTERNAL_RESERVE = 5 * 1024 ** 3
EXTERNAL_RESERVE = 2 * 1024 ** 3
EJECTED = '已安全退出，可以拔除硬碟；重新插上後約 30 秒自動繼續'
DEFAULT = {'enabled': False, 'types': ['iPhone', 'Mac'], 'models': [], 'interval_hours': 0.5, 'cleanup': True, 'cleanup_early': False, 'min_speed_mbps': 2}


class SlowDownload(Exception):
    def __init__(self, offset):
        super().__init__('下載速度低於期望值')
        self.offset = offset


def apple_url(url):
    p = urlparse(url)
    return (p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443)
            and any(p.hostname == h or (p.hostname or '').endswith('.' + h) for h in ('apple.com', 'cdn-apple.com')))


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not apple_url(newurl):
            raise ValueError('拒絕非 Apple HTTPS 下載轉址')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get_json(path):
    with build_opener().open(API + path, timeout=30) as response:
        return json.load(response)


def versions(timeline):
    result = {}
    for day in timeline:
        for release in day.get('releases', []):
            system = release.get('type')
            match = re.fullmatch(r'(?:iOS|iPadOS|macOS) (\d+(?:\.\d+)+) \([^()]+\)', release.get('name', ''))
            if system in SYSTEMS.values() and match:
                version = match[1]
                if tuple(map(int, version.split('.'))) > tuple(map(int, result.get(system, '0').split('.'))):
                    result[system] = version
    return result


def device_type(identifier):
    if re.fullmatch(r'iPhone\d+,\d+', identifier):
        return 'iPhone'
    if re.fullmatch(r'iPad\d+,\d+', identifier):
        return 'iPad'
    if re.fullmatch(r'(?:Mac(?:Book\w*|mini|Pro)?\d+,\d+|iMac\w*\d+,\d+)', identifier):
        return 'Mac'
    return None


def plan_firmwares(items, config, latest):
    groups = {}
    for item in items:
        kind = device_type(item.get('identifier', ''))
        if kind not in config['types'] or item.get('version') != latest.get(SYSTEMS.get(kind)):
            continue
        if config['models'] and item['identifier'] not in config['models']:
            continue
        if item.get('signed') is not True or not apple_url(item.get('url', '')):
            continue
        if not isinstance(item.get('filesize'), int) or item['filesize'] <= 0:
            raise ValueError('API 檔案大小不完整，停止下載')
        if not re.fullmatch(r'[0-9a-fA-F]{40}', item.get('sha1sum', '')):
            raise ValueError('API 缺少有效 SHA-1 校驗值，停止下載')
        url = item['url']
        name = unquote(urlparse(url).path.rsplit('/', 1)[-1])
        if not re.fullmatch(r'[A-Za-z0-9_.,+\-]{1,220}\.ipsw', name):
            raise ValueError('不合法的韌體檔名')
        if url not in groups:
            groups[url] = {'name': name, 'url': url, 'devices': [], 'size': item['filesize'],
                           'sha1': item['sha1sum'].lower(), 'version': item['version'],
                           'build': item['buildid'], 'label': kind + ' ' + item['version']}
        group = groups[url]
        if group['size'] != item['filesize'] or group['sha1'] != item['sha1sum'].lower():
            raise ValueError('同網址的韌體資料不一致')
        group['devices'].append(item['identifier'])
        if re.fullmatch(r'[0-9a-fA-F]{64}', item.get('sha256sum', '')):
            group['api_sha256'] = item['sha256sum'].lower()
    names = [item['name'] for item in groups.values()]
    if len(names) != len(set(names)):
        raise ValueError('不同韌體使用相同檔名，停止以免覆蓋')
    return list(groups.values())


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)


def export_call(request):
    result = subprocess.run(['ssh', '-T', '-i', '/keys/id_ed25519', '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile=/keys/known_hosts', os.environ.get('EXPORT_SSH', 'admin@127.0.0.1')],
        input=json.dumps(request), text=True, capture_output=True, timeout=7200)
    if result.returncode:
        raise ValueError('外接磁碟複製程序無法連線：' + result.stderr[-400:])
    response = json.loads(result.stdout)
    if not response.get('ok'):
        raise ValueError(response.get('error', '外接硬碟操作失敗'))
    return response


if MAC:
    export_call = mac_backend.call


class Service:
    def __init__(self, data=DATA):
        self.data = data
        data.mkdir(parents=True, exist_ok=True)
        self.staging = data / 'staging'
        self.staging.mkdir(exist_ok=True)
        self.config = {**DEFAULT, **self.read('config.json', {})}
        self.records = self.read('records.json', {})
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.token = secrets.token_urlsafe(32)
        self.requested = True
        self.refresh_needed = False
        self.state = {'phase': 'idle', 'message': '尚未啟用自動下載', 'progress': 0,
                      'total': 0, 'latest': {}, 'plan': [], 'added': [], 'last_check': None, 'next_check': None}
        self.disk = {'connected': False, 'error': '尚未檢查外接硬碟'}
        self.tasks = {}
        self.reconnects = {}
        self.verify_lock = threading.Lock()
        self.batch_stop = threading.Event()
        self.batch_active = False
        self.ejected = False
        self.notifications = Notifications(data, export_call)

    def read(self, name, default):
        p = self.data / name
        return json.loads(p.read_text()) if p.exists() else default

    def update(self, **kw):
        with self.lock:
            self.state.update(kw)

    def record(self, item):
        with self.lock:
            self.records[item['name']] = item
            atomic_json(self.data / 'records.json', self.records)

    def staging_bytes(self):
        total = 0
        for path in self.staging.iterdir():
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def snapshot(self):
        try:
            export_progress = self.read('export-progress.json', None)
        except (OSError, ValueError):
            export_progress = None
        with self.lock:
            return copy.deepcopy({'version': VERSION, 'platform': 'mac' if MAC else 'nas', 'config': self.config, 'state': self.state, 'disk': self.disk,
                'records': list(self.records.values()), 'internal_free': shutil.disk_usage(self.data).free,
                'staging_bytes': self.staging_bytes(),
                'token': self.token, 'notifications': self.notifications.status(),
                'transfers': list(self.tasks.values()), 'max_transfers': MAX_TRANSFERS,
                'export_progress': export_progress})

    def observe_notifications(self, timeline, types):
        try:
            self.notifications.observe(timeline, types, self.disk.get('connected', False))
        except Exception:
            self.notifications.error()

    def save_config(self, value):
        value = dict(value)
        value.setdefault('min_speed_mbps', self.config['min_speed_mbps'])
        value.setdefault('cleanup_early', self.config['cleanup_early'])
        if (type(value['cleanup_early']) is not bool or type(value.get('min_speed_mbps')) not in (int, float) or not 0 <= value['min_speed_mbps'] <= 1000 or type(value.get('enabled')) is not bool or type(value.get('cleanup')) is not bool
            or type(value.get('interval_hours')) not in (int, float) or not 0.5 <= value['interval_hours'] <= 168
            or not isinstance(value.get('types'), list) or not value['types']
            or any(x not in SYSTEMS for x in value['types'])
            or not isinstance(value.get('models'), list) or len(value['models']) > 1000
            or any(not isinstance(x, str) or not device_type(x) for x in value['models'])):
            raise ValueError('設定格式錯誤；檢查間隔須為 0.5–168 小時，期望速度須為 0–1000 MB/s')
        with self.lock:
            self.refresh_needed = value['types'] != self.config['types'] or value['models'] != self.config['models']
            self.config = {key: value[key] for key in DEFAULT}
            self.state['next_check'] = time.time() + value['interval_hours'] * 3600 if value['enabled'] else None
            atomic_json(self.data / 'config.json', self.config)
        self.wake.set()

    def enabled(self):
        with self.lock:
            return self.config['enabled']

    def transfer(self, item, phase, progress=0):
        now = time.monotonic()
        with self.lock:
            old = self.tasks.get(item['name'], {})
            start = old.get('_start', now) if old.get('phase') == phase else now
            origin = old.get('_origin', progress) if old.get('phase') == phase else progress
            elapsed = now - start
            speed = max(0, progress - origin) / elapsed if elapsed >= 1 else 0
            self.tasks[item['name']] = {'name': item['name'], 'phase': phase,
                'progress': progress, 'total': item['size'], 'speed_bps': speed,
                'eta_seconds': max(0, item['size'] - progress) / speed if speed else None,
                'reconnects': self.reconnects.get(item['name'], 0),
                '_start': start, '_origin': origin}

    def download_allowed(self):
        if not self.enabled() or (self.batch_active and
                (self.batch_stop.is_set() or not self.disk.get('connected'))):
            raise ValueError('已暫停或外接硬碟未連接，保留暫存供下次續傳')

    def verify_one(self, path, item):
        self.transfer(item, 'waiting_verify', item['size'])
        with self.verify_lock:
            self.download_allowed()
            return self.verify(path, item)

    def download(self, item):
        target = self.staging / item['name']
        partial = self.staging / (item['name'] + '.partial')
        for path in (target, partial):
            if path.is_symlink():
                raise ValueError('暫存路徑不可為符號連結')
        if target.exists():
            if target.stat().st_size != item['size']:
                # Staging holds only files this service wrote; a stale copy would otherwise block forever.
                target.unlink()
            else:
                return self.verify_one(target, item)
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > item['size']:
            raise ValueError('暫存檔大於預期，不覆蓋')
        if shutil.disk_usage(self.staging).free < item['size'] - offset + 5 * 1024 ** 3:
            raise ValueError('內部暫存空間不足，至少保留 5 GiB')
        failures = 0
        while offset < item['size']:
            before = offset
            try:
                offset = self.fetch(item, partial, offset)
            except SlowDownload as slow:
                offset = slow.offset
                self.reconnected(item, 'slow')
            except (OSError, http.client.HTTPException) as e:
                # Network hiccups resume on the same file; repeated failures with no progress stop the batch.
                offset = partial.stat().st_size if partial.exists() else 0
                failures = 0 if offset > before else failures + 1
                if failures >= MAX_NETWORK_RETRIES:
                    raise ValueError('下載連線多次失敗，保留暫存稍後重試：%s' % e)
                self.reconnected(item, 'error')
                time.sleep(min(30, 5 * failures))
        sha256 = self.verify_one(partial, item)
        partial.replace(target)
        return sha256

    def fetch(self, item, partial, offset):
        req = Request(item['url'], headers={'Range': 'bytes=%d-' % offset} if offset else {})
        with build_opener(Redirects()).open(req, timeout=60) as response:
            if not apple_url(response.url):
                raise ValueError('下載來源不是 Apple HTTPS')
            if response.status == 206:
                expected = 'bytes %d-%d/%d' % (offset, item['size'] - 1, item['size'])
                if response.headers.get('Content-Range') != expected:
                    raise ValueError('續傳範圍不符，保留暫存')
            elif response.status == 200:
                offset = 0
            else:
                raise ValueError('不支援的下載回應')
            with self.lock:
                minimum = self.config.get('min_speed_mbps', 0) * 1e6
            window_start, window_offset = time.monotonic(), offset
            with open(partial, 'ab' if offset else 'wb') as output:
                self.transfer(item, 'downloading', offset)
                try:
                    while True:
                        self.download_allowed()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        if offset + len(chunk) > item['size']:
                            raise ValueError('回應超過預期大小，停止下載')
                        # Reserve is checked again while running in case another app consumes space.
                        if shutil.disk_usage(self.staging).free < INTERNAL_RESERVE + MAX_TRANSFERS * len(chunk):
                            raise ValueError('內部暫存空間不足，保留暫存等待重試')
                        output.write(chunk)
                        offset += len(chunk)
                        self.transfer(item, 'downloading', offset)
                        elapsed = time.monotonic() - window_start
                        if elapsed >= SLOW_WINDOW:
                            if minimum and (offset - window_offset) / elapsed < minimum and offset < item['size']:
                                raise SlowDownload(offset)
                            window_start, window_offset = time.monotonic(), offset
                finally:
                    output.flush()
                    os.fsync(output.fileno())
        return offset

    def reconnected(self, item, reason):
        with self.lock:
            count = self.reconnects.get(item['name'], 0) + 1
            self.reconnects[item['name']] = count
            self.state['reconnects'] = self.state.get('reconnects', 0) + 1
        # A new phase restarts the speed average, so the UI shows the new connection's speed.
        self.transfer(item, 'reconnecting', self.tasks.get(item['name'], {}).get('progress', 0))

    def verify(self, path, item):
        self.transfer(item, 'verifying', 0)
        progress = 0
        h1, h256 = hashlib.sha1(), hashlib.sha256()
        with open(path, 'rb') as source:
            for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
                self.download_allowed()
                h1.update(chunk)
                h256.update(chunk)
                progress += len(chunk)
                self.transfer(item, 'verifying', progress)
        if (path.stat().st_size != item['size'] or h1.hexdigest() != item['sha1']
            or (item.get('api_sha256') and h256.hexdigest() != item['api_sha256'])):
            # Only called on staging files this service wrote, never on the external disk.
            path.unlink()
            raise ValueError('韌體大小或雜湊驗證失敗，將重新下載')
        return h256.hexdigest()

    def check(self):
        with self.lock:
            config = copy.deepcopy(self.config)
            self.refresh_needed = False
        now = time.time()
        self.update(phase='checking', message='查詢最新正式版', last_check=now,
                    next_check=now + config['interval_hours'] * 3600 if config['enabled'] else None)
        timeline = get_json('/releases')
        self.observe_notifications(timeline, config['types'])
        latest = versions(timeline)
        items = []
        for version in sorted({latest[SYSTEMS[t]] for t in config['types'] if SYSTEMS[t] in latest}):
            items.extend(get_json('/ipsw/' + version))
        plan = plan_firmwares(items, config, latest)
        self.update(latest=latest, plan=plan)
        if not plan:
            self.update(phase='idle', message='選取範圍沒有目前最新版、已簽署的 IPSW')
            return
        # No downloading while the final disk is absent: avoids an unbounded internal queue.
        if not config['enabled']:
            self.update(phase='idle', message='已查詢；啟用自動下載後才會下載')
            return
        disk = export_call({'action': 'status'})
        with self.lock:
            self.disk = disk
        if sum(item['size'] for item in plan) + 2 * 1024 ** 3 > disk.get('total', 2 ** 63):
            raise ValueError('所選最新版總容量超過外接硬碟容量，請縮小機型範圍；尚未開始下載')
        keep, covered, added = [], set(), []
        self.update(phase='syncing', message='比對外接硬碟上的檔案，只下載缺少的韌體', progress=0, total=len(plan), added=[])
        with self.lock:
            self.disk = disk
            self.tasks = {}
            self.reconnects = {}
            self.batch_active = True
            self.batch_stop.clear()
        try:
            self.sync_plan(plan, config, keep, covered, added)
        finally:
            with self.lock:
                self.batch_active = False
                self.tasks = {}
        with self.lock:
            refreshing = self.refresh_needed
        if len(keep) == len(plan) and added:
            message = '目前最新版已同步至外接硬碟：本次新增 %d 個，%d 個原本已存在' % (len(added), len(plan) - len(added))
        elif len(keep) == len(plan):
            message = '目前最新版都已在外接硬碟，這次不需要下載'
        elif refreshing:
            message = '版本或範圍已變更，正在重建下載清單'
        else:
            message = '已暫停'
        self.update(phase='idle', message=message)
        with self.lock:
            current_scope = self.config['types'] == config['types'] and self.config['models'] == config['models']
            complete = len(keep) == len(plan) and not self.refresh_needed and self.config['enabled'] and current_scope
        if complete:
            try:
                self.notifications.completed(plan, added)
            except Exception:
                self.notifications.error()

    def prepare(self, item):
        sha256 = self.download(item)
        self.record({**item, 'sha256': sha256, 'status': 'waiting_export', 'time': time.time()})
        self.transfer(item, 'waiting_export', item['size'])
        return sha256

    def sync_plan(self, plan, config, keep, covered, added=None):
        added = [] if added is None else added
        downloads = {}    # future -> item: downloading or verifying, at most MAX_TRANSFERS
        backlog = []      # downloaded and verified, waiting for the external disk
        exporting = None  # (future, item): the single copy running on the external disk
        reservations = {}
        next_item = 0
        names = [f['name'] for f in plan]

        def finish(item, synced_at=None):
            (self.staging / item['name']).unlink(missing_ok=True)
            if synced_at is None:
                # Copied to the external disk in this batch.
                synced_at = time.time()
                added.append(item['name'])
            self.record({**item, 'status': 'exported', 'time': synced_at, 'synced_at': synced_at})
            keep.append(item['name'])
            covered.update(item['devices'])
            with self.lock:
                self.tasks.pop(item['name'], None)
            self.update(message='已確認 %d / %d 個檔案：本次新增 %d 個，%d 個原本已在外接硬碟'
                        % (len(keep), len(plan), len(added), len(keep) - len(added)),
                        progress=len(keep), added=list(added))

        def export(item, covered_now):
            # Runs on the single disk worker, so copies and cleanup never overlap on the external disk.
            export_call({'action': 'export', **item})
            if config['cleanup']:
                self.cleanup(names, covered_now)

        def busy():
            return downloads or backlog or exporting

        def purge():
            # Old files superseded by this plan go first, so the new version never waits for space.
            self.update(message='正在清除將被新版取代的舊韌體，同時開始下載')
            self.cleanup(names, {device for f in plan for device in f['devices']})

        with ThreadPoolExecutor(max_workers=MAX_TRANSFERS) as pool, ThreadPoolExecutor(max_workers=1) as disk:
            try:
                if config['cleanup'] and config.get('cleanup_early'):
                    exporting = (disk.submit(purge), None)
                while next_item < len(plan) or busy():
                    # A download slot is refilled as soon as it frees up; copying no longer holds slots.
                    while (next_item < len(plan) and len(downloads) < MAX_TRANSFERS
                           and len(backlog) + bool(exporting) < MAX_BACKLOG
                           and self.enabled() and not self.refresh_needed):
                        item = copy.deepcopy(plan[next_item])
                        previous = self.records.get(item['name'])
                        if previous and previous.get('url') != item['url']:
                            raise ValueError('下載紀錄檔名衝突，停止以免覆蓋')
                        probe = export_call({'action': 'has', **item,
                            'sha256': previous.get('sha256', '') if previous else ''})
                        if probe.get('present'):
                            # The macOS backend reports the hash itself, as it can adopt files it did not download.
                            item['sha256'] = probe.get('sha256') or previous['sha256']
                            previous = previous or {}
                            # Keep the original sync time; the disk file's mtime repairs older records.
                            finish(item, previous.get('synced_at') or probe.get('mtime') or previous.get('time') or time.time())
                            next_item += 1
                            continue
                        target = self.staging / item['name']
                        partial = self.staging / (item['name'] + '.partial')
                        if target.is_symlink() or partial.is_symlink():
                            raise ValueError('暫存路徑不可為符號連結')
                        existing = target.stat().st_size if target.is_file() else partial.stat().st_size if partial.is_file() else 0
                        needed = max(0, item['size'] - existing)
                        # Reservations stay until export, so staging space bounds how far downloads run ahead.
                        internal_ok = shutil.disk_usage(self.staging).free >= needed + sum(x[0] for x in reservations.values()) + INTERNAL_RESERVE
                        external_ok = probe.get('available', 2 ** 63) >= item['size'] + sum(x[1] for x in reservations.values()) + EXTERNAL_RESERVE
                        if not internal_ok or not external_ok:
                            if busy():
                                break  # Export pending files and reclaim their reservations before admitting more.
                            raise ValueError('內部暫存或外接硬碟空間不足，保留舊版與暫存等待重試')
                        reservations[item['name']] = (needed, item['size'])
                        self.record({**item, 'status': 'downloading', 'time': time.time()})
                        self.transfer(item, 'downloading', existing)
                        downloads[pool.submit(self.prepare, item)] = item
                        next_item += 1
                    if not busy():
                        break
                    ready, _ = wait(list(downloads) + ([exporting[0]] if exporting else []),
                                    timeout=1, return_when=FIRST_COMPLETED)
                    if not self.enabled():
                        self.batch_stop.set()
                    # Any failure stops the batch; partial failures never notify completion.
                    for future in [f for f in ready if f in downloads]:
                        item = downloads.pop(future)
                        item['sha256'] = future.result()
                        self.record({**item, 'status': 'waiting_export', 'time': time.time()})
                        self.transfer(item, 'waiting_export', item['size'])
                        backlog.append(item)
                    if exporting and exporting[0].done():
                        future, item = exporting
                        exporting = None
                        future.result()
                        if item:
                            finish(item)
                            del reservations[item['name']]
                    if backlog and not exporting:
                        if not self.enabled():
                            raise ValueError('已暫停，保留內部暫存')
                        item = backlog.pop(0)
                        self.transfer(item, 'exporting', 0)
                        exporting = (disk.submit(export, item, set(covered) | set(item['devices'])), item)
                if config['cleanup'] and keep:
                    self.cleanup(names, covered)
            finally:
                self.batch_stop.set()
                for future in downloads:
                    future.cancel()
                # Executors join running downloads and the current copy before the next plan can touch their files.

    def cleanup(self, keep, covered):
        removed = export_call({'action': 'prune', 'keep': keep, 'covered': sorted(covered)})['removed']
        with self.lock:
            for name in removed:
                if name in self.records:
                    self.records[name]['status'] = 'removed_old'
            for name, old in self.records.items():
                if name not in keep and set(old['devices']).issubset(covered):
                    for candidate in (self.staging / name, self.staging / (name + '.partial')):
                        if candidate.is_file() and not candidate.is_symlink():
                            candidate.unlink()
                    old['status'] = 'removed_old'
            atomic_json(self.data / 'records.json', self.records)

    def eject(self):
        with self.lock:
            if self.batch_active:
                raise ValueError('同步進行中；請先關閉自動下載並儲存，等目前的複製完成後再退出')
        export_call({'action': 'eject'})
        with self.lock:
            self.ejected = True
            self.disk = {'connected': False, 'ejected': True, 'error': EJECTED}

    def mount(self):
        export_call({'action': 'mount'})
        with self.lock:
            self.ejected = False
        self.poll_disk()
        with self.lock:
            if not self.disk.get('connected'):
                raise ValueError(self.disk.get('error') or '重新掛載後仍無法連接外接硬碟')

    def disk_loop(self):
        while True:
            self.poll_disk()
            time.sleep(30)

    def poll_disk(self):
        try:
            disk = export_call({'action': 'status'})
        except Exception as e:
            disk = {'connected': False, 'error': str(e)}
        with self.lock:
            if disk.get('connected'):
                self.ejected = False
            elif self.ejected:
                disk = {'connected': False, 'ejected': True, 'error': EJECTED}
            reconnected = disk.get('connected') and not self.disk.get('connected')
            self.disk = disk
        if reconnected and self.enabled():
            self.wake.set()

    def loop(self):
        while True:
            self.wake.clear()
            failed = False
            try:
                with self.lock:
                    requested, self.requested = self.requested, False
                if self.enabled() or requested:
                    self.check()
                else:
                    self.update(phase='idle', message='自動下載已暫停', next_check=None)
            except Exception as e:
                failed = True
                self.update(phase='waiting', message=str(e))
            self.wake.wait(900 if failed else None)

    def schedule_loop(self):
        # Poll metadata independently, so a multi-hour first sync does not delay version checks.
        while True:
            time.sleep(5)
            self.schedule_tick()

    def schedule_tick(self):
        with self.lock:
            enabled = self.config['enabled']
            due = self.state['next_check']
            interval = self.config['interval_hours'] * 3600
            phase = self.state['phase']
            previous = copy.deepcopy(self.state['latest'])
            types = list(self.config['types'])
        if not enabled or due is None or time.time() < due:
            return
        self.update(next_check=time.time() + interval)
        if phase in ('idle', 'waiting'):
            self.wake.set()
            return
        try:
            timeline = get_json('/releases')
            self.observe_notifications(timeline, types)
            latest = versions(timeline)
            self.update(last_check=time.time(), latest=latest, schedule_error='')
            if latest != previous:
                with self.lock:
                    self.refresh_needed = True
                self.wake.set()
        except Exception:
            self.update(schedule_error='排程查詢暫時失敗，下次重試；目前下載繼續')


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, value, kind='application/json; charset=utf-8'):
            body = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, dict) else value
            if MAC and not kind.startswith('text/css'):
                body = mac_backend.localize(body.decode(), page=not isinstance(value, dict)).encode()
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get('Host') == os.environ.get('PUBLIC_HOST', '127.0.0.1:18766')

        def do_GET(self):
            if not self.allowed_host():
                return self.respond(403, {'error': 'Host 不符'})
            if self.path == '/api/status':
                return self.respond(200, service.snapshot())
            if self.path == '/api/devices':
                try:
                    cached = service.read('devices.json', {})
                    if time.time() - cached.get('time', 0) > 7 * 86400:
                        cached = {'time': time.time(), 'devices': [d for d in get_json('/devices') if device_type(d['identifier'])]}
                        atomic_json(service.data / 'devices.json', cached)
                    return self.respond(200, cached)
                except Exception:
                    return self.respond(502, {'error': '機型清單暫時無法取得，仍可選整個裝置類別'})
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if self.path not in files:
                return self.respond(404, {'error': '找不到頁面'})
            name, kind = files[self.path]
            self.respond(200, (WEB / name).read_bytes(), kind)

        def do_POST(self):
            if (not self.allowed_host() or self.headers.get('X-CSRF-Token') != service.token
                or self.headers.get('Origin', 'http://' + self.headers.get('Host', '')) != 'http://' + self.headers.get('Host', '')):
                return self.respond(403, {'error': '請重新載入頁面後再操作'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16384:
                    raise ValueError('請求大小不符')
                value = json.loads(self.rfile.read(size))
                if self.path == '/api/config':
                    service.save_config(value)
                elif self.path == '/api/check':
                    with service.lock:
                        service.requested = True
                    service.wake.set()
                elif self.path == '/api/disk/eject':
                    service.eject()
                elif self.path == '/api/disk/mount':
                    service.mount()
                elif self.path == '/api/notifications/test':
                    service.notifications.test()
                else:
                    return self.respond(404, {'error': '未知操作'})
                self.respond(200, {'ok': True})
            except (ValueError, KeyError, TypeError) as e:
                self.respond(400, {'error': str(e)})

        def log_message(self, *args):
            pass
    return Handler


if __name__ == '__main__':
    service = Service()
    threading.Thread(target=service.loop, daemon=True).start()
    threading.Thread(target=service.disk_loop, daemon=True).start()
    threading.Thread(target=service.schedule_loop, daemon=True).start()
    threading.Thread(target=service.notifications.loop, daemon=True).start()
    if MAC:
        threading.Thread(target=mac_backend.keep_awake, args=(service,), daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND_ADDRESS', '127.0.0.1'), int(os.environ.get('PORT', '18766'))), make_handler(service)).serve_forever()
