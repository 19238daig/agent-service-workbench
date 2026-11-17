"""Read/replay committed receipts after the final API restart; no new action."""
import asyncio
import json
from datetime import datetime,timezone
import httpx
from dotenv import load_dotenv
from sqlalchemy import select,func
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import ExecutionRun,ModelCallRecord,ActionExecution
from authenticated_client import authenticated_client
from verify_grounded_local import preservation_snapshot


async def counts():
    db=configured_database()
    try:
        async with db.sessions() as s:
            return {m.__tablename__:await s.scalar(select(func.count()).select_from(m)) for m in [ExecutionRun,ModelCallRecord,ActionExecution]}
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH);folder=PROJECT_ROOT/'.local/execution'
    saved=json.loads((folder/'http-resume.json').read_text(encoding='utf-8'));assert saved['passed']
    report={'passed':False,'new_business_write':False,'new_model_calls':False}
    before=asyncio.run(preservation_snapshot());initial=asyncio.run(counts());expired=[]
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=60) as client:
            assert client.get('/health').json()['chat_ready'] and client.get('/').status_code==200
            with authenticated_client(client,'customer-a'):
                for result in [saved['created'],saved['closed']]:
                    path='/api/action-requests/'+result['action_id']
                    original=client.get(path).json()
                    expired.extend(datetime.fromisoformat(a['expires_at'])<=datetime.now(timezone.utc) for a in original['approvals'])
                    assert client.get(path+'/execution').json()==result
                    assert client.post(path+'/execute',json={'parameter_hash':result['parameter_hash']}).json()==result
            with authenticated_client(client,'customer-b'):
                assert client.get('/api/action-requests/'+saved['closed']['action_id']+'/execution').status_code==404
        assert asyncio.run(counts())==initial
        assert asyncio.run(preservation_snapshot())==before
        report.update(passed=True,committed_results_survive_restart=True,expired_consent_not_reused_for_new_writes=True,
            all_original_consents_expired=bool(expired) and all(expired),idempotent_result_replay=True,owner_scoped=True,run_model_and_receipt_counts_unchanged=True,
            current_facts_all_indexes_vectors_and_manifests_preserved=True)
    except Exception as error:report.update(passed=False,error_type=type(error).__name__)
    (folder/'restart-results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
