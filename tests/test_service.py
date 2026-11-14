import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
s = load('server')
e = load('exporter')


class Planning(unittest.TestCase):
    def setUp(self):
        # NAS /tmp can be a tiny RAM filesystem; capacity scenarios are explicit fixtures.
        self.space = patch.object(s.shutil, 'disk_usage', return_value=SimpleNamespace(free=64*1024**3))
        self.space.start()
        self.addCleanup(self.space.stop)

    def item(self, **kw):
        return {'identifier':'iPhone16,1', 'version':'27.0.1', 'buildid':'A', 'signed':True,
                'url':'https://updates.cdn-apple.com/a/Test_Restore.ipsw', 'filesize':4,
                'sha1sum':hashlib.sha1(b'test').hexdigest(), **kw}

    def test_latest_excludes_beta_ota_and_older_branch(self):
        timeline = [{'releases':[{'name':n,'type':t} for n,t in [
            ('iOS 26.7.1 (B)','iOS'),('iOS 27.0.1 (A)','iOS'),
            ('iOS 28.0 beta (X)','iOS'),('iOS OTA 99.0 (X)','iOS OTA')]]}]
        self.assertEqual(s.versions(timeline), {'iOS':'27.0.1'})

    def test_dedupe_filter_and_only_latest_signed(self):
        items = [self.item(), self.item(identifier='iPhone16,2'), self.item(version='26.0'), self.item(signed=False)]
        plan = s.plan_firmwares(items, s.DEFAULT, {'iOS':'27.0.1'})
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]['devices'], ['iPhone16,1','iPhone16,2'])
        config = {**s.DEFAULT,'models':['iPhone16,2']}
        self.assertEqual(s.plan_firmwares(items,config,{'iOS':'27.0.1'})[0]['devices'],['iPhone16,2'])

    def test_untrusted_urls_and_filenames(self):
        for url in ['https://apple.com.evil.test/a.ipsw','https://user@apple.com/a.ipsw','http://apple.com/a.ipsw','file:///tmp/a']:
            self.assertFalse(s.apple_url(url))
        with self.assertRaises(ValueError):
            s.plan_firmwares([self.item(url='https://apple.com/a%2Fbad.ipsw')],s.DEFAULT,{'iOS':'27.0.1'})
        for name in ['../a.ipsw','/tmp/a.ipsw','a/b.ipsw']:
            self.assertFalse(e.valid_name(name))

    def test_absent_disk_never_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            service = s.Service(Path(folder))
            service.config['enabled'] = True
            with patch.object(s,'versions',return_value={'iOS':'27.0.1'}), patch.object(s,'get_json',return_value=[self.item()]), patch.object(s,'export_call',side_effect=ValueError('disk absent')), patch.object(service,'download') as download:
                with self.assertRaises(ValueError): service.check()
                download.assert_not_called()

    def test_export_failure_keeps_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            service = s.Service(Path(folder)); service.config['enabled'] = True
            path = service.staging/'Test_Restore.ipsw'; path.write_bytes(b'test')
            def export(request):
                if request['action']=='status': return {'connected':True}
                if request['action']=='has': return {'present':False}
                raise ValueError('removed during copy')
            with patch.object(s,'versions',return_value={'iOS':'27.0.1'}), patch.object(s,'get_json',return_value=[self.item()]), patch.object(s,'export_call',side_effect=export):
                with self.assertRaises(ValueError): service.check()
            self.assertEqual(path.read_bytes(), b'test')

    def test_oversized_plan_stops_before_download(self):
        with tempfile.TemporaryDirectory() as folder:
            service=s.Service(Path(folder));service.config['enabled']=True
            with patch.object(s,'versions',return_value={'iOS':'27.0.1'}),patch.object(s,'get_json',return_value=[self.item()]),patch.object(s,'export_call',return_value={'total':2}),patch.object(service,'download') as download:
                with self.assertRaisesRegex(ValueError,'總容量'):service.check()
                download.assert_not_called()

    def test_resume_validates_content_range_and_checksum(self):
        class Response(io.BytesIO):
            status=206
            url='https://updates.cdn-apple.com/a/Test_Restore.ipsw'
            headers={'Content-Range':'bytes 2-3/4'}
        with tempfile.TemporaryDirectory() as folder:
            service=s.Service(Path(folder));service.config['enabled']=True
            partial=service.staging/'Test_Restore.ipsw.partial';partial.write_bytes(b'te')
            with patch.object(s,'build_opener') as opener, patch.object(s.shutil,'disk_usage',return_value=SimpleNamespace(free=20*1024**3)):
                opener.return_value.open.return_value=Response(b'st')
                planned=s.plan_firmwares([self.item()],s.DEFAULT,{'iOS':'27.0.1'})[0]
                self.assertEqual(service.download(planned),hashlib.sha256(b'test').hexdigest())
                request=opener.return_value.open.call_args[0][0]
                self.assertEqual(request.headers['Range'],'bytes=2-')
            self.assertFalse(partial.exists())
            self.assertEqual((service.staging/'Test_Restore.ipsw').read_bytes(),b'test')

    def test_corrupt_staged_file_is_removed_for_redownload(self):
        with tempfile.TemporaryDirectory() as folder:
            service=s.Service(Path(folder));service.config['enabled']=True
            target=service.staging/'Test_Restore.ipsw';target.write_bytes(b'tesX')
            planned=s.plan_firmwares([self.item()],s.DEFAULT,{'iOS':'27.0.1'})[0]
            with self.assertRaisesRegex(ValueError,'重新下載'):service.download(planned)
            self.assertFalse(target.exists())

    def test_wrong_size_staged_file_is_replaced(self):
        class Response(io.BytesIO):
            status=200
            url='https://updates.cdn-apple.com/a/Test_Restore.ipsw'
            headers={}
        with tempfile.TemporaryDirectory() as folder:
            service=s.Service(Path(folder));service.config['enabled']=True
            target=service.staging/'Test_Restore.ipsw';target.write_bytes(b'te')
            planned=s.plan_firmwares([self.item()],s.DEFAULT,{'iOS':'27.0.1'})[0]
            with patch.object(s,'build_opener') as opener:
                opener.return_value.open.return_value=Response(b'test')
                self.assertEqual(service.download(planned),hashlib.sha256(b'test').hexdigest())
            self.assertEqual(target.read_bytes(),b'test')

    def test_version_change_mid_batch_reports_rebuild_not_paused(self):
        with tempfile.TemporaryDirectory() as folder:
            service=s.Service(Path(folder));service.config['enabled']=True
            items=[self.item(),self.item(identifier='iPhone16,2',url='https://apple.com/Other_Restore.ipsw')]
            service.records['Test_Restore.ipsw']={'url':items[0]['url'],'sha256':'x','devices':['iPhone16,1']}
            def submit(request):
                if request['action']=='status':return {'connected':True}
                if request['action']=='prune':return {'removed':[]}
                if request['action']=='has':
                    service.refresh_needed=True  # Scheduler saw a new release while this batch ran.
                    return {'present':True}
                raise AssertionError('no further work expected')
            with patch.object(s,'versions',return_value={'iOS':'27.0.1'}),patch.object(s,'get_json',return_value=items),patch.object(s,'export_call',side_effect=submit):
                service.check()
            self.assertEqual(service.state['message'],'版本或範圍已變更，正在重建下載清單')

    def test_existing_files_keep_sync_time_and_only_new_ones_count_as_added(self):
        with tempfile.TemporaryDirectory() as folder:
            service=s.Service(Path(folder));service.config['enabled']=True
            items=[self.item(),self.item(identifier='iPhone16,2',url='https://apple.com/Other_Restore.ipsw')]
            # An older record whose time was overwritten by a later check; the disk mtime is the truth.
            service.records['Test_Restore.ipsw']={'url':items[0]['url'],'sha256':'x','devices':['iPhone16,1'],'status':'exported','time':9999}
            (service.staging/'Other_Restore.ipsw').write_bytes(b'test')
            def submit(request):
                if request['action']=='status':return {'connected':True}
                if request['action']=='prune':return {'removed':[]}
                if request['action']=='has':
                    return {'present':True,'mtime':1234} if request['name']=='Test_Restore.ipsw' else {'present':False}
                return {'exported':True}
            with patch.object(s,'versions',return_value={'iOS':'27.0.1'}),patch.object(s,'get_json',return_value=items),patch.object(s,'export_call',side_effect=submit),patch.object(service.notifications,'completed') as completed:
                service.check()
            existing,new=service.records['Test_Restore.ipsw'],service.records['Other_Restore.ipsw']
            self.assertEqual((existing['synced_at'],existing['time']),(1234,1234))
            self.assertGreater(new['synced_at'],9999)
            self.assertEqual(service.state['added'],['Other_Restore.ipsw'])
            self.assertEqual(service.state['message'],'目前最新版已同步至外接硬碟：本次新增 1 個，1 個原本已存在')
            self.assertEqual(completed.call_args[0][1],['Other_Restore.ipsw'])
            # A later check where everything is already present downloads nothing and keeps sync times.
            service.records['Other_Restore.ipsw']['synced_at']=5678
            with patch.object(s,'versions',return_value={'iOS':'27.0.1'}),patch.object(s,'get_json',return_value=items),patch.object(s,'export_call',side_effect=lambda r:{'connected':True} if r['action']=='status' else {'removed':[]} if r['action']=='prune' else {'present':True}),patch.object(service,'download') as download:
                service.check()
                download.assert_not_called()
            self.assertEqual(service.records['Other_Restore.ipsw']['time'],5678)
            self.assertEqual(service.state['added'],[])
            self.assertEqual(service.state['message'],'目前最新版都已在外接硬碟，這次不需要下載')

    def test_half_hour_config_and_busy_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            service=s.Service(Path(folder))
            service.save_config({**s.DEFAULT,'enabled':True,'interval_hours':0.5})
            service.wake.clear()
            service.update(phase='downloading', next_check=1, latest={'iOS':'27.0'})
            with patch.object(s,'get_json') as query, patch.object(s,'versions',return_value={'iOS':'27.0.1'}),patch.object(s.time,'time',return_value=2000):
                service.schedule_tick()
                query.assert_called_once_with('/releases')
            self.assertEqual(service.state['next_check'],3800)
            self.assertEqual(service.state['phase'],'downloading')
            self.assertTrue(service.refresh_needed)
            self.assertTrue(service.wake.is_set())
            for invalid in (True,0,-1):
                with self.assertRaises(ValueError): service.save_config({**s.DEFAULT,'interval_hours':invalid})

    def test_completion_only_after_entire_export_plan_succeeds(self):
        with tempfile.TemporaryDirectory() as folder:
            service=s.Service(Path(folder));service.config['enabled']=True
            items=[self.item(),self.item(identifier='iPhone16,2',url='https://apple.com/Other_Restore.ipsw')]
            for name in ('Test_Restore.ipsw','Other_Restore.ipsw'):(service.staging/name).write_bytes(b'test')
            def submit(request):
                if request['action']=='status':return {'connected':True}
                if request['action']=='has':return {'present':False}
                if request['action']=='prune':return {'removed':[]}
                if request['name']=='Other_Restore.ipsw':raise ValueError('disk removed')
                return {'exported':True}
            with patch.object(s,'versions',return_value={'iOS':'27.0.1'}),patch.object(s,'get_json',return_value=items),patch.object(s,'export_call',side_effect=submit),patch.object(service.notifications,'completed') as completed:
                with self.assertRaises(ValueError):service.check()
                completed.assert_not_called()
                def success(request):
                    if request['action']=='status':return {'connected':True}
                    if request['action']=='has':return {'present':True}
                    if request['action']=='prune':return {'removed':[]}
                    return {'exported':True}
                service.records['Other_Restore.ipsw']['sha256']='x'
                with patch.object(s,'export_call',side_effect=success):service.check()
                completed.assert_called_once()


@unittest.skipUnless(sys.platform == 'linux', 'Filesystem descriptor pinning requires Linux /proc; run these on NAS')
class DiskCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.base = self.root/'internal'; (self.base/'data/staging').mkdir(parents=True)
        self.disk = self.root/'external'; self.disk.mkdir()
        self.base_patch = patch.object(e,'BASE',self.base); self.base_patch.start()
        self.find_patch = patch.object(e,'find_disk',return_value=self.disk); self.find_patch.start()
        self.mount_patch = patch.object(e.os.path,'ismount',return_value=True); self.mount_patch.start()
        self.payload = {'action':'export','name':'Test_Restore.ipsw','sha256':hashlib.sha256(b'test').hexdigest(),
                        'size':4,'devices':['iPhone16,1'],'version':'27.0.1','build':'A','label':'iPhone'}
        (self.base/'data/staging/Test_Restore.ipsw').write_bytes(b'test')

    def tearDown(self):
        self.mount_patch.stop();self.find_patch.stop();self.base_patch.stop();self.tmp.cleanup()

    def test_copy_verified_and_unmanaged_preserved(self):
        target = self.disk/e.DEST_NAME;target.mkdir();(target/'personal.ipsw').write_bytes(b'keep')
        self.assertTrue(e.run(self.payload)['exported'])
        self.assertEqual((target/self.payload['name']).read_bytes(),b'test')
        probe = e.run({'action':'has',**{k:v for k,v in self.payload.items() if k!='action'}})
        self.assertTrue(probe['present'])
        self.assertEqual(probe['mtime'], (target/self.payload['name']).stat().st_mtime)
        self.assertEqual(e.run({'action':'prune','keep':[],'covered':['iPhone16,1']})['removed'],[self.payload['name']])
        self.assertEqual((target/'personal.ipsw').read_bytes(),b'keep')

    def test_export_progress_separates_copy_flush_readback_and_completion(self):
        updates=[]
        original=e.ExportProgress.update
        def update(progress,stage,count,force=False):
            updates.append((stage,count))
            return original(progress,stage,count,force)
        with patch.object(e.ExportProgress,'update',update):
            e.run(self.payload)
        self.assertIn(('copying',4),updates)
        self.assertIn(('flushing',4),updates)
        self.assertIn(('readback',0),updates)
        self.assertIn(('readback',4),updates)
        state=json.loads((self.base/'data/export-progress.json').read_text())
        self.assertEqual((state['stage'],state['progress'],state['total']),('done',4,4))
        self.assertFalse(state['active'])

    def test_failed_export_never_reports_completed(self):
        self.payload['sha256']='0'*64
        with self.assertRaises(ValueError):e.run(self.payload)
        state=json.loads((self.base/'data/export-progress.json').read_text())
        self.assertEqual(state['stage'],'error')
        self.assertFalse(state['active'])
        self.assertTrue((self.base/'data/staging'/self.payload['name']).exists())

    def test_unmanaged_name_collision_not_overwritten(self):
        target=self.disk/e.DEST_NAME;target.mkdir();(target/self.payload['name']).write_bytes(b'old')
        with self.assertRaisesRegex(ValueError,'非本工具'): e.run(self.payload)
        self.assertEqual((target/self.payload['name']).read_bytes(),b'old')

    def test_disk_removed_before_write_does_not_create_folder(self):
        self.mount_patch.stop()
        with patch.object(e.os.path,'ismount',return_value=False):
            with self.assertRaises(ValueError): e.run(self.payload)
        self.assertFalse((self.disk/e.DEST_NAME).exists())
        self.mount_patch.start()

    def test_bad_hash_retains_source_and_no_final(self):
        self.payload['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'驗證失敗'): e.run(self.payload)
        self.assertTrue((self.base/'data/staging'/self.payload['name']).exists())
        self.assertFalse((self.disk/e.DEST_NAME/self.payload['name']).exists())

    def test_cleanup_waits_for_all_shared_devices(self):
        self.payload['devices']=['iPhone16,1','iPhone16,2'];e.run(self.payload)
        result=e.run({'action':'prune','keep':[],'covered':['iPhone16,1']})
        self.assertEqual(result['removed'],[])

    def test_external_symlink_rejected(self):
        (self.disk/e.DEST_NAME).symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'符號連結'):e.run(self.payload)

    def test_concurrent_exports_are_serialized(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(e.run,[self.payload,self.payload]))
        self.assertTrue(all(r['exported'] for r in results))
        self.assertEqual((self.disk/e.DEST_NAME/self.payload['name']).read_bytes(),b'test')

    def test_owned_failed_partial_is_pruned_after_successor(self):
        self.payload['sha256']='0'*64
        with self.assertRaises(ValueError):e.run(self.payload)
        self.assertTrue((self.disk/e.DEST_NAME/(self.payload['name']+'.partial')).exists())
        e.run({'action':'prune','keep':[],'covered':['iPhone16,1']})
        self.assertFalse((self.disk/e.DEST_NAME/(self.payload['name']+'.partial')).exists())
        self.assertTrue((self.base/'data/staging'/self.payload['name']).exists())

if __name__=='__main__': unittest.main()


class Eject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root/'data').mkdir()
        self.base = patch.object(e, 'BASE', self.root); self.base.start(); self.addCleanup(self.base.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_usb_device_name_from_mounts(self):
        mounts = '/dev/md0 / ext4 rw 0 0\n/dev/usb1p1 /volumeUSB1/usbshare exfat rw 0 0\n'
        with patch.object(e.Path, 'read_text', return_value=mounts):
            self.assertEqual(e.usb_device(Path('/volumeUSB1/usbshare')), 'usb1')
            with self.assertRaises(ValueError): e.usb_device(Path('/volume1'))

    def test_eject_unmounts_with_fixed_dsm_command(self):
        with patch.object(e, 'find_disk', return_value=Path('/volumeUSB1/usbshare')), \
             patch.object(e, 'usb_device', return_value='usb1'), patch.object(e.os, 'sync'), \
             patch.object(e.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as run, \
             patch.object(e.os.path, 'ismount', return_value=False):
            self.assertEqual(e.run({'action': 'eject'}), {'ejected': True, 'device': 'usb1'})
            run.assert_called_once()
            self.assertEqual(run.call_args[0][0], ['sudo', '-n', '/usr/syno/bin/synousbdisk', '-umount', 'usb1'])

    def test_eject_reports_failure_when_still_mounted(self):
        with patch.object(e, 'find_disk', return_value=Path('/volumeUSB1/usbshare')), \
             patch.object(e, 'usb_device', return_value='usb1'), patch.object(e.os, 'sync'), \
             patch.object(e.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='busy')), \
             patch.object(e.os.path, 'ismount', return_value=True):
            with self.assertRaisesRegex(ValueError, '退出失敗'): e.run({'action': 'eject'})

    def test_eject_refused_while_copy_holds_lock(self):
        import fcntl
        with open(self.root/'data'/'export.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with patch.object(e.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, '正在複製'): e.run({'action': 'eject'})
                run.assert_not_called()

    def test_service_eject_blocked_during_sync_and_status_after(self):
        with tempfile.TemporaryDirectory() as folder:
            service = s.Service(Path(folder))
            service.batch_active = True
            with patch.object(s, 'export_call') as call:
                with self.assertRaisesRegex(ValueError, '同步進行中'): service.eject()
                call.assert_not_called()
            service.batch_active = False
            with patch.object(s, 'export_call', return_value={'ejected': True}):
                service.eject()
            self.assertEqual(service.disk['error'], s.EJECTED)
            # The disk poller keeps the friendly message until the disk comes back, then resumes work.
            with patch.object(s, 'export_call', side_effect=ValueError('外接硬碟未連接')):
                service.poll_disk()
            self.assertTrue(service.disk['ejected'])
            service.config['enabled'] = True; service.wake.clear()
            with patch.object(s, 'export_call', return_value={'connected': True}):
                service.poll_disk()
            self.assertTrue(service.disk['connected']); self.assertFalse(service.ejected)
            self.assertTrue(service.wake.is_set())
            self.assertEqual(service.snapshot()['version'], s.VERSION)

    def test_eject_remembers_device_for_remount(self):
        with patch.object(e, 'find_disk', return_value=Path('/volumeUSB1/usbshare')), \
             patch.object(e, 'usb_device', return_value='usb1'), patch.object(e.os, 'sync'), \
             patch.object(e.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')), \
             patch.object(e.os.path, 'ismount', return_value=False):
            e.run({'action': 'eject'})
        self.assertEqual((self.root/'data'/'ejected-device').read_text(), 'usb1')

    def fake_mount(self, target, already_mounted=()):
        # find_disk succeeds only after the device holding our marker (target) is mounted.
        state = {'mounted': None}
        def find_disk():
            if state['mounted'] != target: raise ValueError('未連接')
            return Path('/volumeUSB1/usbshare')
        def run(cmd, **kw):
            state['mounted'] = cmd[-1]; return SimpleNamespace(returncode=0, stdout='', stderr='')
        return (patch.object(e, 'find_disk', side_effect=find_disk), patch.object(e.subprocess, 'run', side_effect=run),
                patch.object(e, 'mounted_sources', return_value=set(already_mounted)))

    def test_mount_tries_remembered_device_first(self):
        (self.root/'data'/'ejected-device').write_text('usb2')
        find, run, mounts = self.fake_mount('usb2')
        with find, run as call, mounts, patch.object(e, 'usb_devices', return_value=['usb1', 'usb2']):
            self.assertEqual(e.run({'action': 'mount'})['device'], 'usb2')
        self.assertEqual([c[0][0][-1] for c in call.call_args_list], ['usb2'])
        self.assertFalse((self.root/'data'/'ejected-device').exists())

    def test_mount_falls_back_when_device_name_changed(self):
        (self.root/'data'/'ejected-device').write_text('usb1')
        find, run, mounts = self.fake_mount('usb3', already_mounted={'/dev/usb2p1'})
        with find, run as call, mounts, patch.object(e, 'usb_devices', return_value=['usb2', 'usb3']):
            self.assertEqual(e.run({'action': 'mount'})['device'], 'usb3')
        # usb1 is gone, usb2 is another disk already mounted and left alone.
        self.assertEqual([c[0][0] for c in call.call_args_list],
                         [['sudo', '-n', '/usr/syno/bin/synousbdisk', '-mount', 'usb1'],
                          ['sudo', '-n', '/usr/syno/bin/synousbdisk', '-mount', 'usb3']])

    def test_mount_skips_when_already_connected_and_errors_when_missing(self):
        with patch.object(e, 'find_disk', return_value=Path('/volumeUSB1/usbshare')), patch.object(e.subprocess, 'run') as run:
            self.assertTrue(e.run({'action': 'mount'})['already'])
            run.assert_not_called()
        find, run, mounts = self.fake_mount('none')
        with find, run, mounts, patch.object(e, 'usb_devices', return_value=['usb1']):
            with self.assertRaisesRegex(ValueError, '重新插上'): e.run({'action': 'mount'})

    def test_service_mount_reconnects_and_clears_ejected(self):
        with tempfile.TemporaryDirectory() as folder:
            service = s.Service(Path(folder)); service.ejected = True; service.config['enabled'] = True
            with patch.object(s, 'export_call', side_effect=lambda r: {'mounted': True} if r['action'] == 'mount' else {'connected': True}):
                service.mount()
            self.assertTrue(service.disk['connected']); self.assertFalse(service.ejected); self.assertTrue(service.wake.is_set())
            with patch.object(s, 'export_call', side_effect=lambda r: {'mounted': True} if r['action'] == 'mount' else (_ for _ in ()).throw(ValueError('外接硬碟未連接'))):
                with self.assertRaisesRegex(ValueError, '未連接'): service.mount()


class SlowReconnect(unittest.TestCase):
    URL = 'https://updates.cdn-apple.com/a/Test_Restore.ipsw'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.service = s.Service(Path(self.tmp.name)); self.service.config['enabled'] = True
        self.item = s.plan_firmwares([{'identifier':'iPhone16,1','version':'27.0.1','buildid':'A','signed':True,
            'url':self.URL,'filesize':4,'sha1sum':hashlib.sha1(b'test').hexdigest()}], s.DEFAULT, {'iOS':'27.0.1'})[0]
        for p in (patch.object(s.shutil,'disk_usage',return_value=SimpleNamespace(free=64*1024**3)), patch.object(s.time,'sleep')):
            p.start(); self.addCleanup(p.stop)
        clock = {'t': 0.0}
        def monotonic():
            clock['t'] += 100  # every call is 100 s later, so a 1-byte read looks very slow
            return clock['t']
        p = patch.object(s.time, 'monotonic', side_effect=monotonic); p.start(); self.addCleanup(p.stop)

    def response(self, data, start=None):
        class Response:
            url = self.URL
            status = 206 if start else 200
            headers = {'Content-Range': 'bytes %d-3/4' % start} if start else {}
            def __init__(self): self.left = data
            def read(self, n):
                chunk, self.left = self.left[:1], self.left[1:]  # one byte per read
                return chunk
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return Response()

    def test_slow_connection_reconnects_and_resumes(self):
        def serve(req, timeout):
            start = int(req.headers['Range'][6:-1]) if 'Range' in req.headers else 0
            return self.response(b'test'[start:], start=start or None)
        with patch.object(s, 'build_opener') as opener:
            opener.return_value.open.side_effect = serve
            self.assertEqual(self.service.download(self.item), hashlib.sha256(b'test').hexdigest())
            requests = [c[0][0] for c in opener.return_value.open.call_args_list]
        self.assertNotIn('Range', requests[0].headers)
        # The slow connection was dropped mid-file and resumed from where it stopped.
        self.assertGreaterEqual(len(requests), 2)
        self.assertRegex(requests[1].headers['Range'], r'^bytes=[123]-$')
        self.assertEqual(self.service.reconnects['Test_Restore.ipsw'], len(requests) - 1)
        self.assertEqual((self.service.staging/'Test_Restore.ipsw').read_bytes(), b'test')

    def test_zero_threshold_never_reconnects(self):
        self.service.config['min_speed_mbps'] = 0
        with patch.object(s, 'build_opener') as opener:
            opener.return_value.open.side_effect = [self.response(b'test')]
            self.service.download(self.item)
        self.assertEqual(self.service.reconnects, {})

    def test_network_error_retries_same_file(self):
        with patch.object(s, 'build_opener') as opener:
            opener.return_value.open.side_effect = [OSError('connection reset'), self.response(b'test')]
            self.service.config['min_speed_mbps'] = 0
            self.assertEqual(self.service.download(self.item), hashlib.sha256(b'test').hexdigest())
        self.assertEqual(self.service.reconnects['Test_Restore.ipsw'], 1)

    def test_repeated_network_errors_without_progress_stop(self):
        with patch.object(s, 'build_opener') as opener:
            opener.return_value.open.side_effect = OSError('offline')
            with self.assertRaisesRegex(ValueError, '多次失敗'): self.service.download(self.item)
            self.assertEqual(opener.return_value.open.call_count, s.MAX_NETWORK_RETRIES)

    def test_speed_setting_validated_and_optional(self):
        base = {k: v for k, v in s.DEFAULT.items() if k != 'min_speed_mbps'}
        self.service.save_config(base)  # older clients omit the field and keep the current value
        self.assertEqual(self.service.config['min_speed_mbps'], 2)
        self.service.save_config({**base, 'min_speed_mbps': 5})
        self.assertEqual(self.service.config['min_speed_mbps'], 5)
        for bad in (-1, 1001, '2', True):
            with self.assertRaises(ValueError): self.service.save_config({**base, 'min_speed_mbps': bad})
