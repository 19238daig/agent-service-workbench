"""Read-only end-to-end model checks against the running local application."""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from authenticated_client import authenticated_async_client
import httpx
from smart_cs.settings import PROJECT_ROOT
from smart_cs.settings import ENV_PATH
from dotenv import load_dotenv


async def verify(output=None):
    load_dotenv(ENV_PATH)
    report={'passed':False,'kind':'real_model_http_smoke','cases':[]}
    path=Path(output) if output else PROJECT_ROOT/'.local/rag/public-http-verification.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8000',trust_env=False,timeout=240) as client, authenticated_async_client(client) as identity:
            for attempt in range(60):
                try:
                    if (await client.get('/health')).json().get('chat_ready'):break
                except httpx.HTTPError:pass
                await asyncio.sleep(.5)
            else:raise RuntimeError('Application not ready')
            foundation=(await client.get('/api/foundation')).json()
            assert foundation['retrieval_mode']=='local_bge_m3_pgvector'
            report['index']=foundation['rag_index']
            documents=(await client.get('/api/knowledge/documents')).json()['documents']
            assert all(d['provenance']['source_type'].startswith('public_') for d in documents)
            corpus=(PROJECT_ROOT/os.getenv('RAG_SOURCE_DIRECTORY','data/public-after-sales-v1/sources')).parent
            manifest=json.loads((corpus/'source_manifest.json').read_text(encoding='utf-8'))
            assert len(documents)==len(manifest['sources'])
            hashes={s['file']:s['sha256'] for s in manifest['sources']}
            for doc in documents:
                response=await client.get(doc['file_url'])
                assert response.status_code==200
                assert hashlib.sha256(response.content).hexdigest()==hashes[doc['source']]
                if doc['source'].endswith('.html'):
                    assert response.headers['content-type'].startswith('text/plain')
                    assert 'attachment' in response.headers['content-disposition']
            for private_path in ['/.env','/data/public-after-sales-v1/source_manifest.json','/api/knowledge/documents/private/file']:
                assert (await client.get(private_path)).status_code==404
            report['original_files_http_hash_verified']=len(documents)
            async def chat(question,expected_source):
                start=time.perf_counter()
                response=await client.post('/api/chat',json={'message':question,'user_id':'public-rag-verification'})
                assert response.status_code==200
                result=response.json()
                assert result['intent']=='knowledge_rag'
                assert result['compliance_passed'] is True
                assert any(c['source']==expected_source for c in result['citations'])
                assert result['agent_steps']==['intent_router','supervisor_route','knowledge_rag','compliance_check','synthesize']
                return {'question':question,'passed':True,'seconds':round(time.perf_counter()-start,2),
                        'agent_steps':result['agent_steps'],'citations':result['citations'],'answer':result['response']}
            cases=[('请根据公开的消费者权益保护法实施条例，说明自动续费服务在何时应提醒消费者。','consumer_protection_regulation.pdf'),
                   ('请根据知识库的网络购买商品七日无理由退货暂行办法，解释哪些商品不适用七日无理由退货。','seven_day_returns.html')]
            report['cases']=await asyncio.gather(*(chat(*case) for case in cases))
            report['passed']=True
    except Exception as error:
        # Do not print provider payloads, environment variables, or credentials.
        report['error_type']=type(error).__name__
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=False,indent=2))
    for case in report['cases']:
        print(json.dumps({k:v for k,v in case.items() if k!='answer'},ensure_ascii=False))
    return report['passed']


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output')
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
