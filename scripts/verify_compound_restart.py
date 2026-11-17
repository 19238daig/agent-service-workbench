"""Final restart: completed plans/history/receipts are read-only and owner-scoped."""
import asyncio
import json
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from authenticated_client import authenticated_client
from verify_execution_receipts_restart import counts
from verify_grounded_local import preservation_snapshot


def main():
    load_dotenv(ENV_PATH);out=PROJECT_ROOT/'.local/tasks'
    saved=json.loads((out/'http-finish.json').read_text(encoding='utf-8'));assert saved['passed']
    initial=asyncio.run(counts());before=asyncio.run(preservation_snapshot())
    report={'passed':False,'new_business_write':False,'new_model_calls':False}
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=60) as client:
            assert client.get('/health').json()['chat_ready'] and client.get('/').status_code==200
            with authenticated_client(client,'customer-a'):
                for result in [saved['committed'],saved['closed']]:
                    history=client.get('/api/history/'+result['session_id']).json()
                    assert history['can_continue'] and not history['can_resume']
                    assert history['task_plan']==result['task_plan'] and history['task_plan']['status']=='completed'
                    assert history['messages'][-1]['metadata']['action_execution']==result['action_execution']
                    for secret in ['slots','goal','replies','context_hash','result']:assert secret not in history['task_plan']
                    action=result['action_execution']
                    assert client.get('/api/action-requests/'+action['action_id']+'/execution').json()==action
                    assert client.post('/api/sessions/'+result['session_id']+'/resume',json={}).status_code==409
            with authenticated_client(client,'customer-b'):
                assert client.get('/api/history/'+saved['committed']['session_id']).status_code==404
        assert asyncio.run(counts())==initial and asyncio.run(preservation_snapshot())==before
        report.update(passed=True,completed_task_plans_and_receipts_survive_restart=True,
            completed_plans_not_replayed=True,only_safe_task_summary_public=True,owner_scoped=True,
            facts_all_indexes_vectors_manifests_unchanged=True,run_model_receipt_counts_unchanged=True)
    except Exception as error:report.update(passed=False,error_type=type(error).__name__)
    (out/'restart-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
