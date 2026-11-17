"""Check the previously created live ticket after restarting the API process."""
import json
import time
import httpx
from smart_cs.settings import PROJECT_ROOT

path = PROJECT_ROOT / '.local/foundation-runtime-verification.json'
report = json.loads(path.read_text(encoding='utf-8'))
if not report.get('passed'):
    raise RuntimeError('Run the live acceptance check first')
with httpx.Client(trust_env=False, timeout=3) as client:
    for attempt in range(40):
        try:
            response = client.get('http://127.0.0.1:8000/api/foundation')
            response.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 39:
                raise
            time.sleep(0.5)
    result = client.get('http://127.0.0.1:8000/api/tickets/' + report['ticket_id'], params={'user_id': report['user_id']})
    result.raise_for_status()
    assert result.json()['ticket']['ticket_id'] == report['ticket_id']
    assert response.json()['counts']['tickets'] == report['final_counts']['tickets']
    assert response.json()['counts']['dataset_samples'] == 670
    report['api_restart_persistence'] = True
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'api_restart_persistence': True, 'ticket_id': report['ticket_id'],
                      'counts': response.json()['counts']}, ensure_ascii=False))
