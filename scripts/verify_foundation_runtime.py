"""Validate live data endpoints and one real-model ticket stored in PostgreSQL."""
import json
import uuid
from pathlib import Path
import httpx

from smart_cs.settings import PROJECT_ROOT

report = {'passed': False, 'user_id': 'foundation-smoke-' + uuid.uuid4().hex[:12]}
try:
    with httpx.Client(trust_env=False, timeout=240) as client:
        base = 'http://127.0.0.1:8000'
        foundation = client.get(base + '/api/foundation')
        foundation.raise_for_status()
        initial = foundation.json()
        assert initial['backend'] == 'postgresql'
        assert initial['counts']['dataset_samples'] == 670
        assert initial['counts']['knowledge_documents'] == 12
        assert initial['retrieval_mode'] == 'lexical_bootstrap_bm25'
        assert len(client.get(base + '/api/orders', params={'user_id': 'demo-user-001'}).json()['orders']) == 10
        assert len(client.get(base + '/api/demo/users').json()['users']) == 20
        for private in ['/.env', '/data/synthetic-after-sales-v1/intent/test.jsonl']:
            assert client.get(base + private).status_code == 404
        events = []
        with client.stream('POST', base + '/api/chat/stream', json={
            'message': '请帮我创建一张普通售后服务工单，摘要是数据库持久化验收，仅登记咨询记录，不要执行资金退款。',
            'user_id': report['user_id']}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith('data: '):
                    event = json.loads(line[6:])
                    events.append(event)
                    print(event['type'], event.get('node', ''), event.get('status', ''), flush=True)
        final = events[-1]
        assert final['type'] == 'complete' and final['intent'] == 'ticket_handler'
        step = next(s for s in final['execution_trace'] if s['node'] == 'ticket_handler')
        assert step['summary']['found']
        ticket_id = step['summary']['ticket_id']
        ticket = client.get(base + '/api/tickets/' + ticket_id, params={'user_id': report['user_id']})
        ticket.raise_for_status()
        assert ticket.json()['ticket']['source'] == 'agent'
        assert client.get(base + '/api/tickets/' + ticket_id, params={'user_id': 'another-workspace'}).status_code == 404
        after = client.get(base + '/api/foundation').json()
        assert after['counts']['tickets'] == initial['counts']['tickets'] + 1
        assert after['counts']['ticket_events'] == initial['counts']['ticket_events'] + 1
        report.update(passed=True, ticket_id=ticket_id, initial_counts=initial['counts'], final_counts=after['counts'],
                      agent_steps=final['agent_steps'], agent_models=final['agent_models'],
                      compliance_passed=final['compliance_passed'], duration_ms=final['duration_ms'],
                      persistent_api_lookup=True, owner_filter=True)
except Exception as error:
    report['error_type'] = type(error).__name__
finally:
    (PROJECT_ROOT / '.local/foundation-runtime-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
raise SystemExit(0 if report['passed'] else 1)
