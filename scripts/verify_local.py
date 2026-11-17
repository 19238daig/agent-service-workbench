"""Verify the running local service without printing provider secrets."""
import json
import re
import sys
from pathlib import Path

from authenticated_client import authenticated_client
import httpx

from smart_cs.settings import PROJECT_ROOT


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--chat', action='store_true', help='Also make real model requests')
    args = parser.parse_args()
    results = []
    with httpx.Client(base_url=args.url, timeout=180, trust_env=False) as client, authenticated_client(client) as identity:
        for path in ['/health', '/docs', '/api/tools', '/api/metrics']:
            r = client.get(path)
            results.append({'path': path, 'status': r.status_code, 'passed': r.is_success})
            if path == '/health' and r.is_success:
                results[-1]['details'] = r.json()
        r = client.post('/api/tools/call', json={
            'name': 'order_query', 'arguments': {'order_id': 'LOCAL-001'}})
        results.append({'path': 'order_query', 'status': r.status_code,
                        'passed': r.is_success and r.json().get('success') is True})
        if args.chat:
            # Real chat checks are read-only from phase nine onward.
            from verify_tickets_http import main as verify_tickets
            from unittest.mock import patch
            with patch('sys.argv',['verify_tickets_http.py','--chat']):
                passed=verify_tickets()==0
            results.append({'path':'authenticated ticket query','passed':passed})
    report = PROJECT_ROOT / '.local' / 'verification.json'
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item['passed'] for item in results) else 1


if __name__ == '__main__':
    sys.exit(main())
