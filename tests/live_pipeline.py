"""Inside deployed container: simulate five small downloads, use real verification and USB export.
Only uniquely named self-test files are created/pruned. No Apple download or email is triggered.
"""
import hashlib
import io
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
from unittest.mock import patch
import server as s

payload = b'IPSW pipeline self test\n' * 182361  # About 4 MiB per file.
run_id = secrets.token_hex(6)
marker = '__IPSW_PIPELINE_SELFTEST_' + run_id + '__'
progress_path = s.DATA / 'export-progress.json'
previous_progress = progress_path.read_bytes() if progress_path.exists() else None
barrier = threading.Barrier(5, timeout=15)
lock = threading.Lock()
stats = {'active_downloads': 0, 'peak_downloads': 0, 'copy_overlapped_download': False}
original_export = s.export_call

class Response(io.BytesIO):
    status = 200
    headers = {}
    def __init__(self, url, delay):
        super().__init__(payload)
        self.url = url
        self.delay = delay
    def read(self, size=-1):
        time.sleep(self.delay)
        return super().read(size)
    def __exit__(self, *args):
        with lock: stats['active_downloads'] -= 1
        return super().__exit__(*args)

class Opener:
    def open(self, request, **kwargs):
        with lock:
            stats['active_downloads'] += 1
            stats['peak_downloads'] = max(stats['peak_downloads'],stats['active_downloads'])
        barrier.wait()
        i = int(request.full_url.rsplit('/',1)[-1].split('.')[0])
        return Response(request.full_url, 0.05 + i * 0.1)

def export(request):
    if request['action'] == 'export':
        with lock:
            stats['copy_overlapped_download'] |= stats['active_downloads'] > 0
    return original_export(request)

with tempfile.TemporaryDirectory() as folder:
    service = s.Service(Path(folder))
    service.staging = s.DATA / 'staging'
    service.config['enabled'] = True
    service.disk = original_export({'action':'status'})
    service.batch_active = True
    items = [{'name':'IPSW_PipelineTest_%s_%d_Restore.ipsw' % (run_id,i),
              'url':'https://updates.cdn-apple.com/selftest/%d.ipsw' % i,
              'size':len(payload),'sha1':hashlib.sha1(payload).hexdigest(),
              'devices':[marker],'version':'test','build':'test','label':'儲存流程測試'} for i in range(5)]
    assert all(not (service.staging/x['name']).exists() and not (service.staging/(x['name']+'.partial')).exists() for x in items)
    kept = []
    try:
        with patch.object(s,'build_opener',return_value=Opener()),patch.object(s,'export_call',side_effect=export):
            service.sync_plan(items,{**service.config,'cleanup':False},kept,set())
        assert stats['peak_downloads'] == 5
        assert stats['copy_overlapped_download']
        assert len(kept) == 5
        assert all(not (service.staging/x['name']).exists() for x in items)
        report = json.loads(progress_path.read_text())
        assert report['stage']=='done' and report['progress']==len(payload)
        print(json.dumps({'simulated_downloads':5,'real_verified_external_exports':len(kept),
            'internal_test_staging_cleared':True,'final_export_stage':report['stage'],**stats}))
    finally:
        original_export({'action':'prune','keep':[],'covered':[marker]})
        for item in items:
            for suffix in ('','.partial'):
                (service.staging/(item['name']+suffix)).unlink(missing_ok=True)
        if previous_progress is not None:
            s.atomic_json(progress_path,json.loads(previous_progress))
        else:
            progress_path.unlink(missing_ok=True)
