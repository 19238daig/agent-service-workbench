"""Live SSE diagnostics with progress printed before waiting for each model."""
import json
from pathlib import Path
import httpx

root = Path(__file__).resolve().parents[1]
(root / '.local').mkdir(exist_ok=True)
report = {'passed': False, 'checks': []}
session = None
try:
    with httpx.Client(trust_env=False, timeout=160) as client:
        for question in ['请帮我创建一张退款申请工单，原因是误购。', '刚才那张工单现在是什么状态？',
                         '我怀疑遇到诈骗，账户里出现了不是我本人操作的扣款，现在该怎么办？']:
            items = []
            with client.stream('POST', 'http://127.0.0.1:8000/api/chat/stream', json={'message': question, 'session_id': session, 'user_id': 'workflow-live-check'}) as response:
                print('HTTP', response.status_code, flush=True)
                for line in response.iter_lines():
                    if line.startswith('data: '):
                        event = json.loads(line[6:])
                        items.append(event)
                        print(event['type'], event.get('node', ''), event.get('status', ''), event.get('duration_ms', ''), flush=True)
            final = items[-1]
            if final['type'] != 'complete':
                raise RuntimeError('Incomplete workflow: ' + final['type'])
            session = final['session_id']
            report['checks'].append(final)
            if not final['compliance_passed']:
                raise RuntimeError('Reply was blocked by review')
        created = report['checks'][0]['execution_trace'][2]['summary']
        queried = report['checks'][1]['execution_trace'][2]['summary']
        assert created['action'] == 'create' and created['found']
        assert queried['action'] == 'query' and queried['found'] and queried['ticket_id'] == created['ticket_id']
        assert report['checks'][2]['intent'] == 'compliance_checker'
        report['passed'] = True
except Exception as error:
    report['error_type'] = type(error).__name__
finally:
    (root / '.local/workflow-live-ticket.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('RESULT', report['passed'], 'checks', len(report['checks']), report.get('error_type', ''), flush=True)
raise SystemExit(0 if report['passed'] else 1)
