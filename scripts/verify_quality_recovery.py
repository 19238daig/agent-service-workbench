"""One explicit supplemental check of a harness fix; never replace primary data."""
import asyncio,json
from datetime import datetime,timezone
from dotenv import load_dotenv
from smart_cs.settings import ENV_PATH,PROJECT_ROOT
from smart_cs.models import build_agent_models
from evaluate_agents import one
from quality_database import create,cleanup,Fixture
from evaluate_quality import OUT,read,write,sha


async def main():
    output=OUT/'supplement/foreign-order--A1.json';protocol=OUT/'supplement/protocol.json'
    if output.exists():raise ValueError('Supplement already recorded; no automatic replay')
    case=next(json.loads(line) for line in (PROJECT_ROOT/'data/agent-workflow-eval-v1/cases.jsonl').read_text(encoding='utf-8').splitlines() if json.loads(line)['id']=='foreign-order')
    write(protocol,{'reason':'Explicit supplement after null-result evidence-collector fix; retains initial primary error and its cost.',
        'source_sha256':sha(PROJECT_ROOT/'scripts/evaluate_agents.py'),'initial_primary_sha256':sha(OUT/'workflows/test/foreign-order--A1.json'),
        'clock':read(OUT/'workflows/test/protocol.json')['clock'],'replace_primary':False})
    write(output,{'id':'foreign-order','arm':'A1','status':'started'})
    db=await create()
    try:
        result=await one(Fixture(db),case,'A1',build_agent_models(),datetime.fromisoformat(read(protocol)['clock']))
        result['supplement_only']=True;write(output,result)
        assert result['status']=='evaluated' and result['after']['executions']==0 and result['after']['tickets']==1
        write(OUT/'supplement/check.json',{'passed':True,'harness_completed_without_crash':True,'business_writes':0,
            'original_primary_error_retained':True,'specific_refusal_task_success':result.get('success'),
            'no_quality_metric_replaced':True})
        print({'supplement_passed':True,'original_primary_error_retained':True,'specific_refusal_success':result.get('success')})
    finally:await db.close();await cleanup()


if __name__=='__main__':load_dotenv(ENV_PATH);asyncio.run(main())
