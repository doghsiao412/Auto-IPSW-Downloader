"""Run inside the deployed container. Creates and removes only a named 64 KiB test file."""
import hashlib
import json
from pathlib import Path
from server import export_call

name = 'IPSW_SelfTest_Restore.ipsw'
source = Path('/data/staging') / name
assert not source.exists(), '同名測試檔已存在，停止以免覆蓋'
payload = b'IPSW storage verification\n' * 2048
source.write_bytes(payload)
request = {'name': name, 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
           'devices': ['__IPSW_SELFTEST__'], 'version': 'test', 'build': 'test', 'label': '儲存驗證測試'}
disk = export_call({'action': 'status'})
result = export_call({'action': 'export', **request})
assert result['exported']
assert export_call({'action': 'has', **request})['present']
source.unlink()
removed = export_call({'action': 'prune', 'keep': [], 'covered': ['__IPSW_SELFTEST__']})
assert name in removed['removed']
assert not export_call({'action': 'has', **request})['present']
print(json.dumps({'external_connected': disk['connected'], 'copy_readback_verified': True,
                  'staging_removed': not source.exists(), 'external_test_removed': True,
                  'test_bytes': len(payload)}, ensure_ascii=False))
