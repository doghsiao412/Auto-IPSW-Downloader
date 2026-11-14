"""DSM Task Scheduler prints this report and sends its native run-details email."""
import json
from pathlib import Path

data = Path('/volume1/docker/ipsw-downloader/data')
request = data / 'dsm-mail-request.json'
if not request.exists():
    print('Auto IPSW Downloader：DSM 郵件通知測試\n\n這封信由 DSM 工作排程器寄出。\n設定完成後會通知新版本及全部 IPSW 同步完成。')
else:
    event = json.loads(request.read_text())
    print(event['title'] + '\n\n' + event['details'], flush=True)
    ack = data / 'dsm-mail-ack.json'
    temporary = ack.with_suffix('.tmp')
    temporary.write_text(json.dumps({'event_id': event['event_id']}))
    temporary.replace(ack)
settings = data.parent / '.env'
host = dict(line.split('=', 1) for line in settings.read_text().splitlines() if '=' in line).get('PUBLIC_HOST') if settings.exists() else None
if host:
    print('\n管理介面：http://%s/' % host)
