"""Direct real Knowledge Agent comparison; routing miss remains separately reported."""
import asyncio
from unittest.mock import patch
import os
from uuid import uuid4
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage,AIMessage
from smart_cs.settings import ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.models import build_agent_models
from smart_cs.agents.knowledge_rag import KnowledgeRAGAgent
from smart_cs.memory.bge_memory import BGEKnowledgeMemory
from smart_cs.observability.context import RunObservation,run_context,usage_summary
from smart_cs.performance.runtime import summary
from accept_performance_live import FIRST,FOLLOW
from evaluate_performance import OUT,read,write

async def main():
    assert not (OUT/'rewrite-live.json').exists(),'Real model comparison already recorded; do not replay'
    db=configured_database();results=[]
    try:
        for mode in ['B3','B4']:
            obs=RunObservation(uuid4().hex,'rewrite-live','rewrite-live');token=run_context.set(obs)
            try:
                with patch.dict(os.environ,{'PERF_ENABLED':'true' if mode=='B4' else 'false'}):
                    memory=BGEKnowledgeMemory(db);model=build_agent_models()['knowledge_rag']
                    value=await KnowledgeRAGAgent(model,memory).process({'messages':[HumanMessage(content=FIRST),AIMessage(content='请继续提出你的问题。'),HumanMessage(content=FOLLOW)]})
                    metrics=summary(obs)
                results.append({'mode':mode,'calls':obs.calls,'usage':usage_summary(obs.calls),'performance':metrics,
                                'knowledge_status':value['knowledge_evidence']['status'],
                                'citations':len(value['knowledge_evidence']['citations'])})
            finally:run_context.reset(token)
        assert sum(c['kind']=='chat' for c in results[0]['calls'])==3
        assert sum(c['kind']=='chat' for c in results[1]['calls'])==2
        assert results[1]['performance']['rewrite_skipped']==1
        write(OUT/'rewrite-live.json',{'passed':True,'scope':'direct Knowledge Agent only; no intent routing or outbound compliance; not full conversation quality','results':results})
        existing=[read(OUT/f'live-B4-{i}.json') for i in range(3)]
        assert existing[1]['performance']['cache_hits']>=2
        write(OUT/'live-B4-summary.json',{'passed':True,'runs':existing,
              'followup_route_did_not_exercise_knowledge':True,
              'limitation':'Third B4 run had low-confidence intent clarification; not counted as rewrite saving or answer success. Direct Agent test reported separately.'})
        print({'passed':True,'direct_chat_calls':[sum(c['kind']=='chat' for c in r['calls']) for r in results],
               'answer_statuses':[r['knowledge_status'] for r in results]})
    finally:await db.close()

if __name__=='__main__':load_dotenv(ENV_PATH);asyncio.run(main())
