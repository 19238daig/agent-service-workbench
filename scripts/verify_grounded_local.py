"""Read-only authenticated chat acceptance for the grounded RAG release path."""
import asyncio
import json
import re
import httpx
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from authenticated_client import authenticated_client
from verify_dense_local import snapshot


async def preservation_snapshot():
    from smart_cs.db.database import configured_database
    from smart_cs.db.models import RetrievalIndex
    from sqlalchemy import select,text
    db=configured_database()
    try:
        async with db.sessions() as session:
            ids=list((await session.scalars(select(RetrievalIndex.id).order_by(RetrievalIndex.id))).all())
            active=list((await session.scalars(select(RetrievalIndex.id).where(RetrievalIndex.active.is_(True)))).all())
            manifests=(await session.execute(text("SELECT count(*),md5(COALESCE(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM retrieval_index_documents t"))).one()
        result=await snapshot(ids)
        return {**result,'active_indexes':active,'manifest_fingerprint':list(manifests)}
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH);folder=PROJECT_ROOT/'.local/grounding';folder.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'real_api':True,'real_chat_models':True,'cases':[],
        'business_write_performed':False,'credentials_or_error_bodies_saved':False}
    before=asyncio.run(preservation_snapshot())
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=300) as client,authenticated_client(client,'customer-b'):
            assert client.get('/').status_code==200
            for case,message,expected in [
                ('renewal','【第十三步只读验收】请依据知识库说明自动续费日期前是否应提醒消费者，保留原文条件和资料来源，不办理业务。','answered'),
                ('missing_address','【第十三步只读验收】请从知识库查找我这家演示店铺的具体退货仓库地址；没有资料请明确说明，不办理业务。','clarify'),
                ('historical_scope','【第十三步只读验收】2020年的中文Zammad手册能证明当前所有版本和本项目都有同样功能吗？仅咨询资料适用范围。',None)]:
                response=client.post('/api/chat/stream',json={'message':message});assert response.status_code==200
                events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                errors=[e.get('code') for e in events if e.get('type')=='error']
                if errors:raise RuntimeError('Grounded chat execution failed: '+','.join(errors))
                final=next(e for e in events if e['type']=='complete')
                assert final['compliance_passed']
                rag_routed='knowledge_rag' in final['agent_steps']
                result=final['knowledge_result']
                if rag_routed:assert result['protocol']=='grounded-rag-v1'
                else:assert case=='historical_scope' and final['intent']=='clarification' and not final['citations']
                if expected:assert result['status']==expected
                citations=final['citations'];anchors=0
                if result.get('status')=='answered':assert citations and result['support_check']=='literal_quote_and_model_review'
                else:assert not citations
                for citation in citations:
                    assert client.get(citation['url'].split('#')[0]).status_code==200
                    block=client.get(citation['block_url']);assert block.status_code==200
                    assert block.headers['cache-control']=='no-store'
                    block=block.json();assert block['currently_effective'] and block['version']==citation['version']
                    assert block['chunk_sha256']==citation['chunk_sha256']
                    for quote in citation['quotes']:
                        assert block['content'][quote['start']:quote['end']]==quote['text'];anchors+=1
                assert set(map(int,re.findall(r'\[(\d+)\]',final['response'])))=={c['number'] for c in citations}
                history=client.get('/api/history/'+final['session_id']).json()
                saved=history['messages'][-1]
                assert saved['role']=='assistant' and saved['content']==final['response']
                assert saved['metadata']['knowledge_result']==result and saved['metadata']['citations']==citations
                run=client.get('/api/runs/'+final['run_id']).json()
                operations=[c.get('operation') for c in run['model_calls']]
                if result.get('status')=='answered':assert 'rag_grounded_draft' in operations and 'rag_support_review' in operations
                row={'id':case,'session_id':final['session_id'],'run_id':final['run_id'],'knowledge_result':result,
                    'response':final['response'],'citations':citations,'verified_quote_anchors':anchors,'rag_routed':rag_routed,
                    'canonical_history_saved':True,'operations':operations,'nodes':final['agent_steps']}
                report['cases'].append(row)
                (folder/'http-verification.partial.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                print(json.dumps({'case':case,'status':result.get('status','clarification_before_rag'),'reason':result.get('reason_code','router_scope_clarification'),'quotes':anchors}),flush=True)
            # Original private acceptance copy remains inaccessible to this user,
            # including the new precise block endpoint; unknown IDs are 404.
            state=json.loads((PROJECT_ROOT/'.local/retrieval/live-case.json').read_text(encoding='utf-8'))
            with client.stream('GET',f"/api/knowledge/documents/{state['private_document_id']}/file") as reply:assert reply.status_code==404
            public=client.post('/api/knowledge/search',json={'query':'自动续费提醒','top_k':1}).json()['documents'][0]
            assert client.get(f"/api/knowledge/documents/{state['private_document_id']}/chunks/{public['id']}").status_code==404
            report['foreign_private_file_and_block_denied']=True
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=10) as client:
            assert client.get('/api/knowledge/documents/unknown/chunks/unknown').status_code==401
        assert asyncio.run(preservation_snapshot())==before
        report.update(passed=True,facts_all_indexes_and_vectors_preserved=True,anonymous_block_access_denied=True)
    except Exception as error:
        report.update(passed=False,error_type=type(error).__name__)
        if 'errors' in locals():report['safe_error_codes']=errors
    (folder/'http-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
