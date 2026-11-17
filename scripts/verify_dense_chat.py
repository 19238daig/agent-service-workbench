"""One explicitly labelled, read-only real-model chat through the dense index."""
import asyncio
import json
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from authenticated_client import authenticated_client
from verify_dense_local import snapshot


def main():
    load_dotenv(ENV_PATH);folder=PROJECT_ROOT/'.local/retrieval'
    report={'passed':False,'real_api':True,'real_chat_models':True,'registration_or_payment_performed':False}
    try:
        state=json.loads((folder/'live-case.json').read_text(encoding='utf-8'))
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=240) as client,authenticated_client(client,'customer-b'):
            response=client.post('/api/chat/stream',json={'message':'【第十二步只读验收】请依据知识库说明：会员订阅到期后平台自动扣费，之前是否应提醒消费者？请标明来源，不办理任何业务。'})
            assert response.status_code==200
            events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
            final=next(e for e in events if e['type']=='complete')
            assert final['compliance_passed'] and final['citations']
            assert 'knowledge_rag' in final['agent_steps']
            assert all(c['document_id']!=state['private_document_id'] for c in final['citations'])
            run=client.get('/api/runs/'+final['run_id']).json()
            kinds=[c['kind'] for c in run['model_calls']]
            assert 'embedding' in kinds and 'reranker' in kinds and kinds.count('chat')>=2
            history=client.get('/api/history/'+final['session_id']).json()
            assert [m['role'] for m in history['messages']]==['user','assistant']
            for citation in final['citations']:assert client.get(citation['url'].split('#')[0]).status_code==200
            report.update(passed=True,session_id=final['session_id'],run_id=final['run_id'],nodes=final['agent_steps'],
                citations=len(final['citations']),model_call_kinds=kinds,private_foreign_evidence_absent=True,canonical_history_saved=True)
        assert asyncio.run(snapshot(state['old_indexes']))==state['before']
        report['facts_and_old_vectors_preserved']=True
    except Exception as error:
        report.update(passed=False,error_type=type(error).__name__)
        if 'events' in locals():report['safe_error_codes']=[e.get('code') for e in events if e.get('type')=='error']
    (folder/'live-chat-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
