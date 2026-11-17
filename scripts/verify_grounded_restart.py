"""Check persisted approved answers and quote blocks after restarting the local API."""
import asyncio
import json
import time
import httpx
from dotenv import load_dotenv
from authenticated_client import authenticated_client
from verify_dense_local import snapshot
from smart_cs.db.database import configured_database
from smart_cs.memory.bge_memory import BGEKnowledgeMemory
from smart_cs.settings import PROJECT_ROOT,ENV_PATH


async def index_status():
    db=configured_database()
    try:return await BGEKnowledgeMemory(db).validate_runtime()
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH);folder=PROJECT_ROOT/'.local/grounding'
    report={'passed':False,'chat_calls_added':0,'replayed_model_or_business_operations':False}
    try:
        previous=json.loads((folder/'http-verification.json').read_text(encoding='utf-8'));assert previous['passed']
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=10) as client:
            # Bounded startup wait, not a retry of any chat or business operation.
            for attempt in range(20):
                try:
                    health=client.get('/health')
                    if health.status_code==200 and health.json().get('chat_ready'):break
                except httpx.ConnectError:pass
                time.sleep(.5)
            else:raise RuntimeError('Local API did not become ready')
            assert client.get('/').status_code==200
            with authenticated_client(client,'customer-b'):
                for case in previous['cases']:
                    history=client.get('/api/history/'+case['session_id']);assert history.status_code==200
                    saved=history.json()['messages'][-1]
                    assert saved['content']==case['response']
                    assert saved['metadata']['knowledge_result']==case['knowledge_result']
                    assert saved['metadata']['citations']==case['citations']
                    for citation in case['citations']:
                        block=client.get(citation['block_url']);assert block.status_code==200
                        block=block.json();assert block['version']==citation['version'] and block['chunk_sha256']==citation['chunk_sha256']
                        for q in citation['quotes']:assert block['content'][q['start']:q['end']]==q['text']
        old=json.loads((PROJECT_ROOT/'.local/retrieval/live-case.json').read_text(encoding='utf-8'))
        assert asyncio.run(snapshot(old['old_indexes']))==old['before']
        index=asyncio.run(index_status());assert index['index_id']==old['index_id']
        report.update(passed=True,api_running=True,chat_ready=True,history_cases=len(previous['cases']),
            approved_answers_and_quotes_survive_restart=True,facts_and_old_vectors_preserved=True,
            active_index_id=index['index_id'],active_documents=index['documents'],active_vectors=index['chunks'],
            actual_embedding_matches_index=True)
    except Exception as error:report.update(passed=False,error_type=type(error).__name__)
    (folder/'restart-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
