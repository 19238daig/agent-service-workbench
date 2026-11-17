"""Real local BGE/pgvector acceptance; keeps fact data and old vectors intact."""
import argparse
import asyncio
import hashlib
import json
import sys
import subprocess
import httpx
from sqlalchemy import select,text,bindparam,func
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import RetrievalIndex,KnowledgeEmbedding,KnowledgeDocument,KnowledgeChunk,KnowledgeIngestion
from smart_cs.retrieval.indexes import DenseIndexBuilder
from smart_cs.retrieval.embedding import EmbeddingAdapter
from smart_cs.retrieval.dense import DenseRetriever
from authenticated_client import authenticated_client

OUTPUT=PROJECT_ROOT/'.local/retrieval'
FACTS=['users','orders','tickets','ticket_events','knowledge_documents','knowledge_chunks','knowledge_ingestions','dataset_releases','dataset_samples','auth_credentials']


async def snapshot(old_ids=None):
    db=configured_database()
    try:
        result={}
        async with db.sessions() as s:
            for name in FACTS:
                projection="to_jsonb(t) - 'embedding_status'" if name=='knowledge_chunks' else 'to_jsonb(t)'
                row=(await s.execute(text(f"SELECT count(*),md5(COALESCE(string_agg(({projection})::text,E'\\n' ORDER BY ({projection})::text),'')) FROM {name} t"))).one()
                result[name]={'count':row[0],'digest':row[1]}
            if old_ids is not None:
                for name,column,projection in [('retrieval_indexes','id',"to_jsonb(t) - 'active'"),('knowledge_embeddings','index_id','to_jsonb(t)')]:
                    statement=text(f"SELECT count(*),md5(COALESCE(string_agg(({projection})::text,E'\\n' ORDER BY ({projection})::text),'')) FROM {name} t WHERE t.{column} IN :ids").bindparams(bindparam('ids',expanding=True))
                    row=(await s.execute(statement,{'ids':old_ids})).one();result[name]={'count':row[0],'digest':row[1]}
        return result
    finally:await db.close()


def cli(name,arguments):
    path=OUTPUT/(name+'.json')
    result=subprocess.run([sys.executable,'-m','smart_cs.rag_cli','--report',path.relative_to(PROJECT_ROOT).as_posix(),*arguments],capture_output=True,timeout=240,cwd=PROJECT_ROOT)
    report=json.loads(path.read_text(encoding='utf-8'))
    if result.returncode or not report['passed']:raise RuntimeError('Local retrieval operation failed: '+name)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['activate','http'])
    parser.add_argument('--after-restart',action='store_true');args=parser.parse_args();load_dotenv(ENV_PATH)
    OUTPUT.mkdir(parents=True,exist_ok=True);state_path=OUTPUT/'live-case.json';report={'passed':False,'action':args.action,'paid_models':False}
    try:
        if args.action=='activate':
            if state_path.exists():raise RuntimeError('Acceptance case already exists')
            staged=json.loads((OUTPUT/'staged-index.json').read_text(encoding='utf-8'));assert staged['passed'] and not staged['active']
            async def old_indexes():
                db=configured_database()
                try:
                    async with db.sessions() as s:
                        old=list((await s.scalars(select(RetrievalIndex.id).where(RetrievalIndex.id!=staged['index_id']))).all())
                        active=await s.scalar(select(RetrievalIndex.id).where(RetrievalIndex.active.is_(True)))
                        return old,active
                finally:await db.close()
            old,active=asyncio.run(old_indexes());assert active==staged['previous_index_id']
            before=asyncio.run(snapshot(old))
            repeated=cli('staged-repeat',['index']);assert repeated['unchanged'] and repeated['index_id']==staged['index_id']
            switched=cli('activation',['activate','--index-id',staged['index_id'],'--expected-active',active])
            assert switched['active'] and asyncio.run(snapshot(old))==before
            again=cli('active-repeat',['index']);assert again['unchanged'] and again['active']
            phase_eleven=json.loads((PROJECT_ROOT/'.local/ingestion/live-case.json').read_text(encoding='utf-8'))
            async def inspect():
                db=configured_database()
                try:
                    async with db.sessions() as s:
                        private_count=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).join(KnowledgeChunk,
                            KnowledgeChunk.id==KnowledgeEmbedding.chunk_id).where(KnowledgeEmbedding.index_id==staged['index_id'],KnowledgeChunk.document_id==phase_eleven['document_ids'][1]))
                        draft_count=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).join(KnowledgeChunk,
                            KnowledgeChunk.id==KnowledgeEmbedding.chunk_id).where(KnowledgeEmbedding.index_id==staged['index_id'],KnowledgeChunk.document_id==phase_eleven['document_ids'][0]))
                    return private_count,draft_count
                finally:await db.close()
            private,draft=asyncio.run(inspect());assert private==94 and draft==0
            state={'before':before,'old_indexes':old,'index_id':staged['index_id'],'private_document_id':phase_eleven['document_ids'][1],
                'draft_document_id':phase_eleven['document_ids'][0],'previous_index_id':active}
            state_path.write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
            report.update(passed=True,index_id=staged['index_id'],documents=staged['documents'],chunks=staged['chunks'],
                private_generic_chunks=private,draft_chunks=draft,old_indexes_and_vectors_preserved=True,
                facts_and_source_chunks_preserved=True,repeat_build_unchanged=True)
        else:
            state=json.loads(state_path.read_text(encoding='utf-8'))
            query='消费者接受自动续费服务后，商家应该在什么时候提醒？'
            results={}
            for login in ['customer-a','customer-b','support','admin']:
                with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=180) as client,authenticated_client(client,login):
                    response=client.post('/api/knowledge/search',json={'query':query,'top_k':50})
                    assert response.status_code==200
                    body=response.json();assert body['search']=='exact_cosine' and body['index_id']==state['index_id'] and body['evidence_only']
                    assert response.headers['cache-control']=='no-store'
                    ids={d['metadata']['document_id'] for d in body['documents']}
                    if login!='customer-a':assert state['private_document_id'] not in ids
                    else:assert state['private_document_id'] in ids
                    assert state['draft_document_id'] not in ids
                    for payload in [{'user_id':'someone-else'},{'as_of':'2030-01-01'},{'index_id':state['previous_index_id']}]:
                        assert client.post('/api/knowledge/search',json={'query':query,**payload}).status_code==422
                    results[login]={'returned':len(body['documents']),'private_in_results':state['private_document_id'] in ids}
            with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=10) as client:
                assert client.post('/api/knowledge/search',json={'query':query}).status_code==401
            assert asyncio.run(snapshot(state['old_indexes']))==state['before']
            report.update(passed=True,after_restart=args.after_restart,identity_filters=results,actor_date_index_override_rejected=True,
                anonymous_denied=True,no_shared_cache=True,facts_and_old_vectors_preserved=True)
    except Exception as error:report['error_type']=type(error).__name__
    (OUTPUT/('restart-verification.json' if args.after_restart else args.action+'-verification.json')).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
