"""Exercise the four-agent paths through the running FastAPI service."""
import json
from pathlib import Path
import re
import sys

import httpx
from dotenv import dotenv_values

from smart_cs.settings import PROJECT_ROOT


def main():
    results = []
    config_values = dotenv_values(PROJECT_ROOT / '.env')
    model_prefixes = {'intent_router': 'INTENT', 'knowledge_rag': 'KNOWLEDGE', 'ticket_handler': 'TICKET', 'compliance_checker': 'COMPLIANCE'}
    selected_models = {role: (config_values.get(prefix + '_MODEL_NAME') or config_values.get('MODEL_NAME') or 'gpt-4o') for role, prefix in model_prefixes.items()}
    session_id = None
    ticket_id = None
    cases = [
        ('knowledge', '购买后第3天能否无理由退款，退款多久能到账？', 'knowledge_rag'),
        ('ticket_create', '请帮我创建一张退款申请工单', 'ticket_handler'),
        ('ticket_followup', '刚才那张退款工单现在是什么状态？', 'ticket_handler'),
        ('security', '有人索要验证码，我怀疑账户异常，应该怎样核实？', 'compliance_checker'),
    ]
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=180, trust_env=False) as client:
        for name, question, expected in cases:
            if name == 'ticket_followup' and not ticket_id:
                results.append({'case': name, 'passed': False, 'error': 'No ticket created in the preceding case.'})
                continue
            try:
                response = client.post('/api/chat', json={
                    'message': question, 'user_id': 'four_agent_verification',
                    'session_id': None if name == 'security' else session_id,
                })
                data = response.json()
                expected_path = ['intent_router', 'supervisor_route']
                if expected != 'compliance_checker':
                    expected_path.append(expected)
                expected_path += ['compliance_check', 'synthesize']
                passed = response.is_success and data.get('intent') == expected and data.get('agent_steps') == expected_path and data.get('compliance_passed') is True
                expected_roles = {'intent_router', 'compliance_checker'} | ({expected} if expected != 'compliance_checker' else set())
                actual_models = data.get('agent_models', {})
                passed = passed and set(actual_models) == expected_roles and all(actual_models[role] == selected_models[role] for role in expected_roles)
                answer = data.get('response', '')
                if name == 'knowledge':
                    passed = passed and '退款' in answer
                if name == 'ticket_create':
                    match = re.search(r'TK-\d{8}-[A-F0-9]{6}', answer)
                    ticket_id = match.group() if match else None
                    passed = passed and bool(ticket_id) and '类型: refund' in answer
                if name == 'ticket_followup':
                    passed = passed and ticket_id in answer and '工单查询结果' in answer
                if name == 'security':
                    passed = passed and bool(answer) and '暂时无法' not in answer and '已冻结' not in answer
                results.append({'case': name, 'status': response.status_code, 'passed': bool(passed), **data})
                if name != 'security' and response.is_success:
                    session_id = data.get('session_id', session_id)
                print(json.dumps({'case': name, 'status': response.status_code, 'passed': bool(passed),
                                  'agent_steps': data.get('agent_steps'), 'agent_models': data.get('agent_models'), 'classification': data.get('classification')}, ensure_ascii=False), flush=True)
            except (httpx.HTTPError, ValueError) as error:
                results.append({'case': name, 'passed': False, 'error': type(error).__name__})
                print(f'{name}: {type(error).__name__}', flush=True)
            report = PROJECT_ROOT / '.local' / 'four-agent-verification.json'
            report.parent.mkdir(exist_ok=True)
            report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if len(results) == len(cases) and all(item['passed'] for item in results) else 1


if __name__ == '__main__':
    sys.exit(main())
