"""Small source-anchored integration probes, not a human-labelled benchmark."""
import asyncio
import hashlib
import json
import os
from dotenv import load_dotenv
from smart_cs.settings import ENV_PATH, PROJECT_ROOT
from smart_cs.db.database import configured_database
from smart_cs.memory.bge_memory import BGEKnowledgeMemory

CASES = [
    ('renewal', '消费者接受自动续费服务，经营者应该在什么时候提醒？', 'consumer_protection_regulation.pdf', 3),
    ('returns', '七日无理由退货哪些商品不适用？', 'seven_day_returns.html', None),
    ('merge', 'Zammad 工单合并时应该如何处理？', 'zammad_user_zh_archived.pdf', 29),
    ('state', 'What are the New and Open ticket states in Zammad?', 'zammad_ticket_basics.html', None),
    ('out_of_scope', '火星基地核聚变燃料价格是多少？', None, None),
]


async def verify():
    load_dotenv(ENV_PATH)
    manifest_path=(PROJECT_ROOT/os.getenv('RAG_SOURCE_DIRECTORY','data/public-after-sales-v1/sources')).parent/'source_manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    for source in manifest['sources']:
        raw=(manifest_path.parent/'sources'/source['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==source['sha256']
    database=configured_database()
    if database is None:raise RuntimeError('Configured database required')
    report={'kind':'integration_smoke','human_labelled_benchmark':False,'cases':[]}
    try:
        memory=BGEKnowledgeMemory(database);report['index']=await memory.describe()
        for case_id,question,source,page in CASES:
            candidates=await memory.search(question,top_k=20)
            results=await memory.rerank(question,candidates,top_k=3)
            passed=any(d['source']==source and (page is None or d['metadata']['page']==page) for d in results) if source else not results
            report['cases'].append({'id':case_id,'question':question,'passed':passed,'results':[
                {'source':d['source'],'page':d['metadata']['page'],'heading':d['metadata']['heading'],
                 'url':d['metadata']['url'],'score':d['rerank_score']} for d in results]})
        report['passed']=all(c['passed'] for c in report['cases'])
        path=PROJECT_ROOT/'.local/rag/public-retrieval-verification.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return report['passed']
    finally:await database.close()


if __name__=='__main__':raise SystemExit(0 if asyncio.run(verify()) else 1)
