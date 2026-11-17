"""Real-model conflict controls, explicitly synthetic and never indexed/imported."""
import asyncio
import json
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.models import build_agent_models
from smart_cs.observability.context import RunObservation,run_context
from smart_cs.retrieval.grounding import GroundedAnswerService


def fixture(identifier,text,**dates):
    return {'id':identifier,'content':text,'source':'SYNTHETIC_ACCEPTANCE_ONLY',
        'metadata':{'document_id':identifier,'version':'acceptance-only','heading':'同一演示店铺当前规则',
            'collection':'synthetic_control_never_indexed','index_id':'not-a-database-index',
            'effective_from':'2020-01-01','effective_until':None,**dates}}


async def run():
    folder=PROJECT_ROOT/'.local/grounding';folder.mkdir(parents=True,exist_ok=True)
    observation=RunObservation('grounding-control','grounding-control','grounding-control');token=run_context.set(observation)
    engine=GroundedAnswerService(build_agent_models()['knowledge_rag'])
    report={'passed':False,'real_model':True,'synthetic_controls':True,'database_modified':False,
        'fixtures_added_to_knowledge_base':False,'quality_benchmark':False,'cases':[]}
    cases=[
        ('contradictory_current_rules','仅根据这两份同一店铺、同一适用日期的规则，究竟由谁承担无理由退货运费？',[
            fixture('control-a','本条为同一演示店铺同一有效期的无理由退货运费规则：全部退货运费由商家承担，消费者无需承担。'),
            fixture('control-b','本条为同一演示店铺同一有效期的无理由退货运费规则：全部退货运费由消费者承担，商家无需承担。')]),
        ('expired_rule','当前退货运费由谁承担？',[fixture('expired','过期验收规则：无理由退货运费由商家承担。',effective_until='2021-01-01')]),
        ('empty_context','请给出未知店铺的退货地址。',[])]
    try:
        for name,query,documents in cases:
            before=len(observation.calls);audit={}
            result=await engine.answer(query,documents,audit=audit)
            passed=result['status']=='clarify' and not result['citations']
            if name=='contradictory_current_rules':passed=passed and result['reason_code']=='conflict'
            if name=='expired_rule':passed=passed and result['reason_code']=='not_current' and len(observation.calls)==before
            if name=='empty_context':passed=passed and result['reason_code']=='no_evidence' and len(observation.calls)==before
            report['cases'].append({'id':name,'passed':passed,'result':result,'chat_calls':len(observation.calls)-before,'structured_audit':audit})
        report['passed']=all(c['passed'] for c in report['cases'])
    finally:
        run_context.reset(token)
        (folder/'real-controls.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'cases':[{k:c[k] for k in ['id','passed','chat_calls']} for c in report['cases']]}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    load_dotenv(ENV_PATH);raise SystemExit(asyncio.run(run()))
