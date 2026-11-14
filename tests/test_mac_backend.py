"""macOS backend tests use tiny files in temporary folders; no real iTunes folder, mail or network."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mac_backend as m

BODY = b'firmware'


def item(name='iPhone_27.0.1_Restore.ipsw', label='iPhone 27.0.1', devices=('iPhone16,1',), body=BODY):
    return {'name': name, 'label': label, 'devices': list(devices), 'size': len(body), 'version': '27.0.1', 'build': 'A',
            'sha1': hashlib.sha1(body).hexdigest(), 'sha256': hashlib.sha256(body).hexdigest(), 'url': 'https://apple.com/' + name}


class MacBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.patches = [patch.object(m, 'DATA', base / 'data'), patch.object(m, 'ROOT', base / 'iTunes')]
        for p in self.patches:
            p.start()
        (m.DATA / 'staging').mkdir(parents=True)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def stage(self, it, body=BODY):
        (m.DATA / 'staging' / it['name']).write_bytes(body)

    def test_each_device_type_lands_in_its_itunes_folder(self):
        for label, folder in (('iPhone 27.0.1', 'iPhone Software Updates'), ('iPad 27.0.1', 'iPad Software Updates'), ('Mac 27.0.1', 'Mac Software Updates')):
            it = item(name=label.split()[0] + '_Restore.ipsw', label=label)
            self.stage(it)
            m.call({'action': 'export', **it})
            self.assertEqual((m.ROOT / folder / it['name']).read_bytes(), BODY)
            self.assertFalse((m.DATA / 'staging' / it['name']).exists())
            self.assertTrue(m.call({'action': 'has', **it})['present'])

    def test_missing_file_is_not_present_and_never_limits_space(self):
        probe = m.call({'action': 'has', **item()})
        self.assertFalse(probe['present'])
        self.assertGreater(probe['available'], 2 ** 60)

    def test_file_already_placed_by_finder_is_adopted_only_when_hash_matches(self):
        it = item()
        folder = m.ROOT / 'iPhone Software Updates'
        folder.mkdir(parents=True)
        (folder / it['name']).write_bytes(b'FIRMWARE')  # same size, different content
        self.assertFalse(m.call({'action': 'has', **it})['present'])
        (folder / it['name']).write_bytes(BODY)
        probe = m.call({'action': 'has', **it, 'sha256': ''})
        self.assertTrue(probe['present'])
        self.assertEqual(probe['sha256'], it['sha256'])

    def test_unmanaged_same_name_file_is_never_overwritten(self):
        it = item()
        folder = m.ROOT / 'iPhone Software Updates'
        folder.mkdir(parents=True)
        (folder / it['name']).write_bytes(b'other!!!')
        self.stage(it)
        with self.assertRaises(ValueError):
            m.call({'action': 'export', **it})
        self.assertEqual((folder / it['name']).read_bytes(), b'other!!!')
        self.assertTrue((m.DATA / 'staging' / it['name']).exists())

    def test_prune_removes_only_superseded_managed_files(self):
        old, new = item(name='Old_Restore.ipsw'), item(name='New_Restore.ipsw')
        other = item(name='Other_Restore.ipsw', devices=('iPhone15,2',))
        for it in (old, new, other):
            self.stage(it)
            m.call({'action': 'export', **it})
        folder = m.ROOT / 'iPhone Software Updates'
        (folder / 'Mine_Restore.ipsw').write_bytes(b'user file')
        removed = m.call({'action': 'prune', 'keep': [new['name']], 'covered': ['iPhone16,1']})['removed']
        self.assertEqual(removed, [old['name']])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['Mine_Restore.ipsw', 'New_Restore.ipsw', 'Other_Restore.ipsw'])

    def test_prune_leaves_a_file_the_user_replaced(self):
        old = item(name='Old_Restore.ipsw')
        self.stage(old)
        m.call({'action': 'export', **old})
        path = m.ROOT / 'iPhone Software Updates' / old['name']
        path.write_bytes(b'replaced by user')
        m.call({'action': 'prune', 'keep': [], 'covered': ['iPhone16,1']})
        self.assertTrue(path.exists())

    def test_eject_and_mount_are_refused(self):
        for action in ('eject', 'mount'):
            with self.assertRaises(ValueError):
                m.call({'action': action})

    def test_notification_shows_banner_once_and_retries_only_mail(self):
        (m.DATA / 'mail.json').write_text(json.dumps({'host': 'smtp.example.com', 'port': 587, 'username': 'a@example.com', 'to': 'b@example.com'}))
        request = {'action': 'notify', 'kind': 'complete', 'event_id': 'a' * 64, 'details': '已同步至外接硬碟\n\n細節'}
        with patch.object(m, 'banner') as banner, patch.object(m, 'send_mail', side_effect=OSError('offline')) as mail:
            with self.assertRaises(ValueError):
                m.call(request)
            self.assertEqual(banner.call_count, 1)
            self.assertIn('iTunes 資料夾', banner.call_args[0][1])
            mail.side_effect = None
            self.assertTrue(m.call(request)['mail'])
            m.call(request)
            self.assertEqual(banner.call_count, 1)
            self.assertEqual(mail.call_count, 2)

    def test_notification_without_mail_settings_still_succeeds(self):
        with patch.object(m, 'banner') as banner:
            result = m.call({'action': 'notify', 'kind': 'test', 'event_id': 'b' * 64, 'details': '測試'})
        self.assertEqual(result, {'submitted': True, 'mail': False})
        banner.assert_called_once()

    def test_wording_is_adapted_for_the_mac_page(self):
        self.assertEqual(m.localize('已同步至外接硬碟'), '已同步至iTunes 資料夾')
        self.assertNotIn('NAS', m.localize('NAS 狀態讀取失敗', page=True))
        self.assertEqual(m.localize('tokenNASx'), 'tokenNASx')


if __name__ == '__main__':
    unittest.main()
