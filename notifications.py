"""Persistent event outbox. DSM owns all mail settings and credentials."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import secrets
import threading
import time

SYSTEMS = {'iPhone': 'iOS', 'iPad': 'iPadOS', 'Mac': 'macOS'}


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def releases(timeline, types):
    result = {}
    for day in timeline:
        for release in day.get('releases', []):
            system = release.get('type')
            match = re.fullmatch(r'(?:iOS|iPadOS|macOS) (\d+(?:\.\d+)+) \(([^()]+)\)', release.get('name', ''))
            if system not in {SYSTEMS[t] for t in types} or not match:
                continue
            version, build = match.groups()
            item = {'version': version, 'build': build, 'date': release.get('date', day.get('date', ''))}
            order = lambda x: (tuple(map(int, x['version'].split('.'))), x['date'])
            if system not in result or order(item) > order(result[system]):
                result[system] = item
    return result


class Notifications:
    def __init__(self, data, submit):
        self.path = data / 'notifications.json'
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.submit = submit
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'baseline': {}, 'events': {}, 'last_submitted': None, 'last_error': ''}

    def save(self):
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(self.state, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        tmp.replace(self.path)

    def queue(self, kind, value, details):
        key = identity([kind, value])
        if key not in self.state['events']:
            self.state['events'][key] = {'kind': kind, 'details': details, 'created': time.time(),
                'status': 'pending', 'attempts': 0, 'retry_at': 0}
            self.wake.set()

    def observe(self, timeline, types, connected):
        found = releases(timeline, types)
        with self.lock:
            baseline = self.state['baseline']
            changed = {}
            for system, item in found.items():
                old = baseline.get(system)
                if old:
                    new_order = (tuple(map(int, item['version'].split('.'))), item['date'])
                    old_order = (tuple(map(int, old['version'].split('.'))), old['date'])
                    if new_order >= old_order and (item['version'], item['build']) != (old['version'], old['build']):
                        changed[system] = item
                if old is None or (tuple(map(int, item['version'].split('.'))), item['date']) >= (tuple(map(int, old['version'].split('.'))), old['date']):
                    baseline[system] = item
            if changed:
                text = '\n'.join('%s %s（Build %s）' % (k, v['version'], v['build']) for k, v in changed.items())
                text += '\n\n' + ('服務會依目前設定同步所選範圍。' if connected else '外接硬碟未連接，等待硬碟接回後下載。')
                self.queue('new', {k: (v['version'], v['build']) for k, v in changed.items()}, text)
            # Baseline and outbox are committed together; current versions are silent on first run.
            self.save()

    def completed(self, plan, added=None):
        if not plan:
            return
        added = set(added or [])
        new = [x for x in plan if x.get('name') in added]
        versions = sorted({x['label'] + '（Build ' + x['build'] + '）' for x in plan})
        if new:
            summary = ('本次新增 %d 個檔案（%.2f GB）：\n' % (len(new), sum(x['size'] for x in new) / 1e9) +
                       '\n'.join('・' + x['name'] for x in sorted(new, key=lambda x: x['name'])) +
                       '\n\n其餘 %d 個檔案原本已在外接硬碟，沒有重新下載。' % (len(plan) - len(new)))
        else:
            summary = '本次沒有新增檔案，所有最新版原本已在外接硬碟。'
        details = ('所選最新版 IPSW 已全部存入外接硬碟並通過驗證，這批內部韌體暫存已清除。\n\n' + summary +
                   '\n\n外接硬碟目前的最新版：\n' + '\n'.join(versions) +
                   '\n共 %d 個檔案，%.2f GB\n外接硬碟目錄：IPSW-Auto' %
                   (len(plan), sum(x['size'] for x in plan) / 1e9))
        with self.lock:
            self.queue('complete', sorted((x['url'], x['sha1']) for x in plan), details)
            self.save()

    def test(self):
        with self.lock:
            self.queue('test', secrets.token_hex(16), 'Auto IPSW Downloader 原生通知測試。\n後續會通知：偵測到新版本，以及全部所選 IPSW 已同步至外接硬碟。')
            self.save()

    def status(self):
        with self.lock:
            return {'channel': 'DSM', 'pending': sum(e['status'] == 'pending' for e in self.state['events'].values()),
                    'last_submitted': self.state['last_submitted'], 'last_error': self.state['last_error']}

    def error(self):
        with self.lock:
            self.state['last_error'] = '通知紀錄暫時無法更新，請檢查內部磁碟；韌體下載繼續'

    def dispatch_once(self):
        with self.lock:
            ready = [(k, e) for k, e in self.state['events'].items() if e['status'] == 'pending' and e['retry_at'] <= time.time()]
            if not ready:
                return
            key, event = ready[0]
            event = copy.deepcopy(event)
        stamp = datetime.fromtimestamp(event['created'], timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S 台北時間')
        try:
            self.submit({'action': 'notify', 'kind': event['kind'], 'event_id': key,
                         'details': event['details'] + '\n\n事件時間：' + stamp})
            error = ''
        except Exception:
            error = 'DSM 通知提交失敗，稍後自動重試；韌體下載繼續'
        with self.lock:
            target = self.state['events'][key]
            target['attempts'] += 1
            self.state['last_error'] = error
            if error:
                target['retry_at'] = time.time() + min(300 * target['attempts'], 3600)
            else:
                target['status'] = 'submitted'
                self.state['last_submitted'] = time.time()
            self.save()

    def loop(self):
        while True:
            self.wake.clear()
            try:
                self.dispatch_once()
            except Exception:
                with self.lock:
                    self.state['last_error'] = '通知紀錄無法更新，請檢查內部磁碟空間'
            self.wake.wait(10)
