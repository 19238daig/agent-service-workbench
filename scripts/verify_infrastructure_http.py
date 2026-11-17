"""Read-only acceptance of live/readiness/status endpoints; no paid LLM calls."""
import json
from pathlib import Path
import time
from authenticated_client import authenticated_client
import httpx
from smart_cs.settings import PROJECT_ROOT


def main():
    report={'passed':False,'paid_calls_performed':False}
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=20) as client, authenticated_client(client) as identity:
            for attempt in range(80):
                try:
                    if client.get('/health/live').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.25)
            else:raise RuntimeError('Liveness unavailable')
            basic=client.get('/health').json()
            assert basic['scope']=='api_configuration_only' and basic['upstream_model_health']=='not_probed'
            ready=client.get('/health/ready')
            assert ready.status_code==200
            data=ready.json();assert data['ready'] and data['profile']=='connectivity' and not data['host_checks_included']
            assert data['checks']['embedding']['details']['inference']=='not_probed'
            assert data['checks']['chat_providers']['details']['paid_calls_performed'] is False
            details=client.get('/api/dependencies').json()
            assert details['checked_at']==data['checked_at']
            report.update(passed=True,liveness=True,readiness=True,cache_timestamp_consistent=True,dependencies=data)
    except Exception as error:report['error_type']=type(error).__name__
    folder=PROJECT_ROOT/'.local/infrastructure';folder.mkdir(parents=True,exist_ok=True)
    (folder/'http-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='dependencies'},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
