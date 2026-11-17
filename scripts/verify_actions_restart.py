"""Verify saved plans after a separately performed project API restart."""
import asyncio
import json
import time
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from authenticated_client import authenticated_client
from verify_grounded_local import preservation_snapshot
from verify_grounded_restart import index_status


def main():
    load_dotenv(ENV_PATH);folder=PROJECT_ROOT/'.local/actions'
    report={'passed':False,'model_calls_replayed':0,'business_writes_performed':False}
    before=asyncio.run(preservation_snapshot())
    try:
        previous=json.loads((folder/'http-verification.json').read_text(encoding='utf-8'));assert previous['passed']
        for attempt in range(30):
            try:
                with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=2) as probe:
                    if probe.get('/health').json().get('chat_ready'):break
            except (httpx.HTTPError,ValueError):pass
            time.sleep(.5)
        else:raise RuntimeError('API not ready')
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=15) as client,authenticated_client(client,'customer-a'):
            assert client.get('/').status_code==200
            for case in previous['cases']:
                history=client.get('/api/history/'+case['session_id']);assert history.status_code==200
                saved=history.json()['messages'][-1]
                assert saved['content']==case['response'] and saved['metadata']['action_plan']==case['action_plan']
                detail=client.get('/api/sessions/'+case['session_id']).json();assert detail['can_continue'] and not detail['can_resume']
        assert asyncio.run(preservation_snapshot())==before
        index=asyncio.run(index_status())
        report.update(passed=True,api_running=True,chat_ready=True,history_cases=len(previous['cases']),
            plans_ids_hashes_and_final_messages_survive_restart=True,completed_sessions_can_continue=True,
            facts_all_indexes_vectors_and_manifests_preserved=True,active_documents=index['documents'],active_vectors=index['chunks'])
    except Exception as error:report.update(passed=False,error_type=type(error).__name__)
    (folder/'restart-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
