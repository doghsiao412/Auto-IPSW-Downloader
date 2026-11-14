"""Concurrency tests use events and tiny files; no firmware or network traffic."""
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = s.Service(Path(self.tmp.name))
        self.service.config['enabled'] = True
        self.service.disk = {'connected': True}
        self.service.batch_active = True
        self.items = [{'name': 'File%d_Restore.ipsw' % i, 'url': 'https://apple.com/%d.ipsw' % i,
                       'size': 4, 'sha1': hashlib.sha1(b'test').hexdigest(), 'version': '27.0.1',
                       'devices': ['iPhone16,%d' % i], 'build': 'A', 'label': 'iPhone 27.0.1'} for i in range(8)]
        self.config = {**self.service.config, 'cleanup': False}
        self.space = patch.object(s.shutil, 'disk_usage', return_value=SimpleNamespace(free=64*1024**3))
        self.space.start()

    def tearDown(self):
        self.space.stop()
        self.tmp.cleanup()

    def stored(self, item):
        (self.service.staging / item['name']).write_bytes(b'test')
        return hashlib.sha256(b'test').hexdigest()

    def test_five_downloads_at_once_and_slots_refill_during_copy(self):
        barrier = threading.Barrier(5, timeout=5)
        refilled = threading.Event()
        lock = threading.Lock()
        downloading, peaks = [0], []
        def download(item):
            i = self.items.index(item)
            with lock:
                downloading[0] += 1; peaks.append(downloading[0])
            try:
                if i < 5:
                    barrier.wait()  # the first five really run at the same time
                else:
                    refilled.set()  # a sixth file started: a finished download's slot was reused
                return self.stored(item)
            finally:
                with lock: downloading[0] -= 1
        def submit(request):
            if request['action'] == 'has': return {'present': False}
            self.assertEqual(request['action'], 'export')
            # The copy to the external disk must not hold up new downloads.
            self.assertTrue(refilled.wait(5), 'No new download started while a copy was running')
            return {'exported': True}
        keep, covered = [], set()
        with patch.object(self.service, 'download', side_effect=download), patch.object(s, 'export_call', side_effect=submit):
            self.service.sync_plan(self.items, self.config, keep, covered)
        self.assertEqual(max(peaks), 5)
        self.assertEqual(set(keep), {x['name'] for x in self.items})
        self.assertFalse(any(self.service.staging.iterdir()))

    def test_backlog_limit_pauses_new_downloads(self):
        release = threading.Event()
        started = []
        def download(item):
            started.append(item['name']); return self.stored(item)
        def submit(request):
            if request['action'] == 'has': return {'present': False}
            release.wait(5); return {'exported': True}
        with patch.object(s, 'MAX_BACKLOG', 2), patch.object(self.service, 'download', side_effect=download), patch.object(s, 'export_call', side_effect=submit):
            worker = threading.Thread(target=self.service.sync_plan, args=(self.items, self.config, [], set()))
            worker.start()
            deadline = time.time() + 3
            while len(started) < 3 and time.time() < deadline: time.sleep(0.05)
            time.sleep(0.3)
            # One copying plus one waiting fills the backlog of 2; at most 5 more may be downloading meanwhile.
            self.assertLess(len(started), len(self.items))
            release.set(); worker.join(10)
        self.assertEqual(len(started), len(self.items))

    def test_early_cleanup_prunes_before_copies_while_downloads_run(self):
        events, downloading = [], threading.Event()
        def download(item):
            downloading.set(); return self.stored(item)
        def submit(request):
            if request['action'] == 'has': return {'present': False}
            if request['action'] == 'prune':
                if not events:
                    self.assertTrue(downloading.wait(5), 'Downloads waited for the old files to be removed')
                events.append(('prune', tuple(request['keep']), tuple(request['covered'])))
                return {'removed': []}
            events.append(('export', request['name'])); return {'exported': True}
        items = self.items[:3]
        with patch.object(self.service, 'download', side_effect=download), patch.object(s, 'export_call', side_effect=submit):
            self.service.sync_plan(items, {**self.config, 'cleanup': True, 'cleanup_early': True}, [], set())
        # The first disk job removes everything this plan supersedes, before any copy.
        self.assertEqual(events[0], ('prune', tuple(x['name'] for x in items), tuple(sorted(d for x in items for d in x['devices']))))
        self.assertEqual(sum(e[0] == 'export' for e in events), 3)

    def test_default_cleanup_never_prunes_before_first_copy(self):
        events = []
        def submit(request):
            if request['action'] == 'has': return {'present': False}
            events.append(request['action']); return {'removed': []} if request['action'] == 'prune' else {'exported': True}
        with patch.object(self.service, 'download', side_effect=self.stored), patch.object(s, 'export_call', side_effect=submit):
            self.service.sync_plan(self.items[:2], {**self.config, 'cleanup': True}, [], set())
        self.assertEqual(events[0], 'export')

    def test_reservations_drain_before_admitting_more(self):
        events = []
        def download(item):
            events.append(('download', item['name']))
            return self.stored(item)
        def submit(request):
            if request['action'] == 'has': return {'present': False}
            events.append(('export', request['name']))
            return {'exported': True}
        with patch.object(self.service, 'download', side_effect=download), patch.object(s, 'export_call', side_effect=submit), patch.object(s.shutil, 'disk_usage', return_value=SimpleNamespace(free=s.INTERNAL_RESERVE + 6)):
            self.service.sync_plan(self.items[:2], self.config, [], set())
        self.assertEqual(events, [('download',self.items[0]['name']),('export',self.items[0]['name']),
                                  ('download',self.items[1]['name']),('export',self.items[1]['name'])])

    def test_external_reservations_also_bound_admission(self):
        events = []
        def download(item): events.append('download'); return self.stored(item)
        def submit(request):
            if request['action'] == 'has': return {'present':False, 'available':s.EXTERNAL_RESERVE+6}
            events.append('export'); return {'exported':True}
        with patch.object(self.service,'download',side_effect=download),patch.object(s,'export_call',side_effect=submit):
            self.service.sync_plan(self.items[:2],self.config,[],set())
        self.assertEqual(events,['download','export','download','export'])

    def test_copy_failure_preserves_all_prepared_files(self):
        all_ready = threading.Barrier(5, timeout=5)
        def download(item):
            result = self.stored(item)
            if self.items.index(item) < 5: all_ready.wait()
            return result
        def submit(request):
            if request['action'] == 'has': return {'present': False}
            raise ValueError('USB removed')
        with patch.object(self.service, 'download', side_effect=download), patch.object(s, 'export_call', side_effect=submit):
            with self.assertRaisesRegex(ValueError, 'USB removed'):
                self.service.sync_plan(self.items, self.config, [], set())
        # Every file that finished downloading stays in staging for the retry.
        self.assertGreaterEqual(len(list(self.service.staging.glob('*.ipsw'))),5)
        self.assertTrue(self.service.batch_stop.is_set())

    def test_usb_disconnected_stops_active_reads_without_deleting_partial(self):
        partial = self.service.staging / (self.items[0]['name']+'.partial')
        partial.write_bytes(b'te')
        self.service.disk = {'connected':False}
        with self.assertRaisesRegex(ValueError,'外接硬碟'):
            self.service.download_allowed()
        self.assertEqual(partial.read_bytes(),b'te')

    def test_verification_is_single_file_and_waits_without_blocking_download_pool(self):
        entered = threading.Event(); release = threading.Event(); second_waiting = threading.Event()
        count = [0,0]
        def verify(path,item):
            count[0]+=1;count[1]=max(count)
            if item==self.items[0]:entered.set();self.assertTrue(release.wait(5))
            count[0]-=1;return 'hash'
        def second():
            second_waiting.set();self.service.verify_one(Path('unused'),self.items[1])
        with patch.object(self.service,'verify',side_effect=verify):
            a=threading.Thread(target=self.service.verify_one,args=(Path('unused'),self.items[0]))
            a.start();self.assertTrue(entered.wait(5))
            b=threading.Thread(target=second);b.start();self.assertTrue(second_waiting.wait(5))
            release.set();a.join(5);b.join(5)
            self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(count[1],1)

    def test_paused_batch_keeps_ready_staging_without_export(self):
        def download(item):
            result=self.stored(item);self.service.config['enabled']=False;return result
        def submit(request):
            self.assertEqual(request['action'],'has');return {'present':False}
        with patch.object(self.service,'download',side_effect=download),patch.object(s,'export_call',side_effect=submit):
            with self.assertRaisesRegex(ValueError,'暫停'):
                self.service.sync_plan(self.items[:1],self.config,[],set())
        self.assertEqual((self.service.staging/self.items[0]['name']).read_bytes(),b'test')


if __name__ == '__main__': unittest.main()
