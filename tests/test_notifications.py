import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from notifications import Notifications

spec = importlib.util.spec_from_file_location('exporter', ROOT / 'exporter.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def timeline(version='27.0.1', build='A', date='2026-10-01'):
    return [{'date': date, 'releases': [{'type':'iOS', 'name':'iOS %s (%s)' % (version,build), 'date':date}]}]


class EventNotifications(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.send = Mock(return_value={'submitted':True})
        self.notices = Notifications(self.data, self.send)

    def tearDown(self): self.temp.cleanup()

    def test_baseline_silent_new_release_once_across_restart(self):
        self.notices.observe(timeline(),['iPhone'],False)
        self.assertEqual(self.notices.status()['pending'],0)
        self.notices.observe(timeline('27.0.2','B','2026-10-02'),['iPhone'],False)
        self.assertEqual(self.notices.status()['pending'],1)
        self.notices.dispatch_once()
        self.assertIn('等待硬碟',self.send.call_args[0][0]['details'])
        restarted = Notifications(self.data,self.send)
        restarted.observe(timeline('27.0.2','B','2026-10-02'),['iPhone'],True)
        restarted.dispatch_once()
        self.assertEqual(self.send.call_count,1)

    def test_old_release_and_unselected_system_not_notified(self):
        self.notices.observe(timeline('27.0.2','B','2026-10-02'),['iPhone'],True)
        self.notices.observe(timeline(),['iPhone'],True)
        self.notices.observe(timeline('28.0','C','2026-11-01'),['Mac'],True)
        self.assertEqual(self.notices.status()['pending'],0)

    def test_completion_dedupe_same_files_even_reordered(self):
        plan=[{'url':'https://apple.com/%s.ipsw'%v,'sha1':v,'label':'iPhone 27.0.1','build':'A','size':4} for v in ('a','b')]
        self.notices.completed(plan)
        self.notices.dispatch_once()
        self.notices.completed(list(reversed(plan)))
        self.notices.dispatch_once()
        self.assertEqual(self.send.call_count,1)
        self.assertIn('共 2 個檔案',self.send.call_args[0][0]['details'])

    def test_completion_lists_only_new_files_as_added(self):
        plan=[{'name':'%s.ipsw'%v,'url':'https://apple.com/%s.ipsw'%v,'sha1':v,'label':'iPhone 27.0.1','build':'A','size':10**9} for v in ('a','b','c')]
        self.notices.completed(plan,['c.ipsw'])
        self.notices.dispatch_once()
        details=self.send.call_args[0][0]['details']
        self.assertIn('本次新增 1 個檔案（1.00 GB）',details)
        self.assertIn('・c.ipsw',details)
        self.assertNotIn('・a.ipsw',details)
        self.assertIn('其餘 2 個檔案原本已在外接硬碟',details)

    def test_failure_retries_without_losing_event(self):
        self.notices.test()
        self.send.side_effect=RuntimeError('native unavailable')
        with patch('notifications.time.time',return_value=1000):self.notices.dispatch_once()
        self.assertEqual(self.notices.status()['pending'],1)
        self.send.side_effect=None
        with patch('notifications.time.time',return_value=1301):self.notices.dispatch_once()
        self.assertEqual(self.notices.status()['pending'],0)
        self.assertEqual(self.send.call_count,2)


class NativeSubmission(unittest.TestCase):
    def test_notify_works_without_usb_and_only_triggers_own_task_once(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'data').mkdir();(base/'dsm-task.json').write_text('{"id":42}')
            request={'action':'notify','kind':'test','event_id':'a'*64,'details':'測試'}
            def run(args,**kw):
                self.assertEqual(args,['sudo','-n','/usr/syno/bin/synoschedtask','--run','id=42','check_time=0','check_status=0'])
                (base/'data/dsm-mail-ack.json').write_text(json.dumps({'event_id':'a'*64}))
                return SimpleNamespace(returncode=0)
            with patch.object(exporter,'BASE',base),patch.object(exporter.subprocess,'run',side_effect=run) as native,patch.object(exporter,'find_disk',side_effect=AssertionError('must not require disk')):
                self.assertTrue(exporter.run(request)['submitted'])
                self.assertTrue(exporter.run(request)['duplicate'])
                self.assertEqual(native.call_count,1)

    def test_cannot_invoke_arbitrary_event_or_task(self):
        with self.assertRaises(ValueError):exporter.notify({'kind':'arbitrary','event_id':'b'*64,'details':'x'})


if __name__=='__main__':unittest.main()
