"""Full frozen B4 component test: 300 intent, 59 RAG and 40 compliance candidates."""
import argparse,asyncio,hashlib,json,os,random
from datetime import date,datetime,timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage,AIMessage
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.evaluation.v2_assets import rows,validate,encoded
from smart_cs.evaluation.v2_schema import IntentV2,RagV2,ComplianceV2
from smart_cs.evaluation.v2_runner import intent_score,valid_intent_format
from smart_cs.evaluation.runner import evidence_scores
from smart_cs.evaluation.quality import aggregate
from smart_cs.models import build_agent_models,resolve_agent_model_settings
from smart_cs.agents.intent_router import IntentRouterAgent
from smart_cs.services.compliance import ComplianceReviewService
from smart_cs.memory.bge_memory import BGEKnowledgeMemory
from smart_cs.retrieval.grounding import GroundedAnswerService
from smart_cs.performance.policy import ServingPolicy
from smart_cs.observability.context import RunObservation,run_context
from evaluate_hybrid import read,write,sha

OUT=PROJECT_ROOT/'.local/quality';DAY=date(2026,11,15)


def code():
    paths=[Path(__file__).resolve(),PROJECT_ROOT/'src/smart_cs/evaluation/quality.py']
    for folder in ['agents','services','retrieval','performance','observability','schemas','memory']:
        paths+=sorted((PROJECT_ROOT/'src/smart_cs'/folder).glob('*.py'))
    paths += [PROJECT_ROOT/'src/smart_cs/models.py',PROJECT_ROOT/'src/smart_cs/performance/profile.json',
              PROJECT_ROOT/'src/smart_cs/retrieval/resources/rerank_profile.json',PROJECT_ROOT/'requirements.lock.txt']
    return {p.relative_to(PROJECT_ROOT).as_posix():sha(p) for p in paths}


async def binding(db):
    memory=BGEKnowledgeMemory(db,clock=lambda:DAY)
    async with db.sessions() as s:index=await memory.retriever().active(s)
    assert await memory.retriever().adapter.aidentity()==index.configuration['embedding_contract']
    roles={}
    for role in ['intent_router','knowledge_rag','ticket_handler','compliance_checker']:
        settings=resolve_agent_model_settings(role)
        roles[role]={'model':settings.model_name,'timeout':settings.timeout,'temperature':settings.temperature,
                     'endpoint_sha256':hashlib.sha256((settings.base_url or '').encode()).hexdigest()}
    return {'dataset':validate(),'index_id':index.id,'index_configuration_sha256':hashlib.sha256(encoded(index.configuration)).hexdigest(),
            'as_of':DAY.isoformat(),'models':roles,'code_sha256':code(),'scope':'public-only component test; no business writes',
            'human_gold':False,'thresholds_or_prompts_tuned_on_test':False}


class Captured:
    def __init__(self,model):self.model=model;self.model_name=model.model_name;self.content=None
    async def ainvoke(self,*args,**kwargs):
        response=await self.model.bind(max_tokens=ServingPolicy.deployed().max_output_tokens).ainvoke(*args,**kwargs)
        self.content=response.content;return response


async def run(concurrency):
    db=configured_database();models=build_agent_models();OUT.mkdir(exist_ok=True)
    try:
        bound=await binding(db)
        if (OUT/'component-protocol.json').exists():assert read(OUT/'component-protocol.json')['binding']==bound
        else:write(OUT/'component-protocol.json',{'binding':bound,'selected_splits':['test'],'concurrency':concurrency,
                 'created_at':datetime.now(timezone.utc).isoformat(),'metrics':'Errors stay in denominators; format errors never become correct unknown; no independent human correctness claim.'})
        selected=[r for r in rows() if r.split=='test' and isinstance(r,(IntentV2,RagV2,ComplianceV2))]
        random.Random('phase26-fixed-component-order').shuffle(selected)
        gate=asyncio.Semaphore(concurrency);local=asyncio.Semaphore(1);dedup={};results=[]
        for path in (OUT/'components').glob('*.json') if (OUT/'components').exists() else []:
            previous=read(path)
            if previous['status']=='started':raise ValueError('Interrupted provider call retained; explicit recovery required, no automatic replay')
            if previous.get('review'):dedup[previous['review_binding']]=previous
        async def one(case):
            async with gate:
                path=OUT/'components'/(case.id+'.json')
                if path.exists():return read(path)
                obs=RunObservation(uuid4().hex,'phase26-component','phase26-component');token=run_context.set(obs);started=perf_counter()
                row={'id':case.id,'task':case.task,'split':case.split,'group_id':case.group_id,'status':'started'}
                # Labels are fixed before invoking a provider, including error paths.
                if isinstance(case,IntentV2):row.update(expected_route=case.expected_route,allowed_routes=case.allowed_routes)
                elif isinstance(case,ComplianceV2):row.update(expected=case.expected_decision)
                else:row.update(case_type=case.case_type,answerable=case.answerable,evaluation_scope=case.evaluation_scope)
                write(path,row)
                try:
                    if isinstance(case,IntentV2):
                        captured=Captured(models['intent_router']);history=[(HumanMessage if m['role']=='user' else AIMessage)(content=m['content']) for m in case.history]
                        value=await IntentRouterAgent(captured).classify(case.message,history);row.update(**intent_score(case,value),format_valid=valid_intent_format(captured.content))
                        if not row['format_valid']:row.update(route_correct=False,joint_correct=False,predicted_route='__error__')
                    elif isinstance(case,ComplianceV2):
                        key=hashlib.sha256(encoded([case.candidate_answer,case.state])).hexdigest()
                        row.update(expected=case.expected_decision,review_binding=key)
                        if key in dedup:
                            old=dedup[key]
                            if isinstance(old,asyncio.Future):old=await old
                            row.update(review=old['review'],predicted=old['predicted'],reviewer=old['reviewer'],reused_from=old['id'])
                        else:
                            future=asyncio.get_running_loop().create_future();dedup[key]=future
                            try:
                                value=(await ComplianceReviewService(models['compliance_checker']).review_content(case.candidate_answer,case.state)).model_dump(mode='json')
                                row.update(review=value,predicted=value['decision'],reviewer=value['reviewer']);future.set_result(dict(row))
                            except BaseException as error:future.set_exception(error);raise
                        if row['reviewer']=='fail_closed':raise ValueError('Unavailable review, not quality success')
                    else:
                        row.update(case_type=case.case_type,answerable=case.answerable,evaluation_scope=case.evaluation_scope)
                        if case.control.get('kind')=='temporal_metadata':
                            control=case.control;checker=GroundedAnswerService(None,clock=lambda:date.fromisoformat(control['as_of']))
                            row.update(control_correct=checker.valid_dates([{'metadata':{'effective_from':control['effective_from'],'effective_until':control['effective_until']}}])==control['expected_valid'],scope='deterministic date control, no generation')
                        else:
                            memory=BGEKnowledgeMemory(db,clock=lambda:DAY)
                            async with local:
                                candidates=await memory.search(case.message,memory.candidate_k);context=await memory.rerank(case.message,candidates,memory.context_k)
                            if case.evaluation_scope=='retrieval':
                                original=read(PROJECT_ROOT/'.local/performance/test'/(case.id+'.json'))['B4_warm']['context_ids']
                                assert [d['id'] for d in context]==original,'B4 evidence changed'
                            canary='PHASE26_CANARY_'+hashlib.sha256(case.id.encode()).hexdigest()[:12]
                            if case.untrusted_context and context:context=[{**context[0],'untrusted_instruction':case.untrusted_context+'\n'+canary},*context[1:]]
                            audit={};answer=await GroundedAnswerService(models['knowledge_rag'],revalidate=memory.revalidate,clock=lambda:DAY).answer(case.message,context,audit=audit)
                            if answer['citations']:await memory.retriever().revalidate_citations(answer['citations'])
                            cited={c['chunk_id'] for c in answer['citations']}
                            row.update(result=answer,answer_status=answer['status'],reason_code=answer['reason_code'],audit=audit,
                                       context_ids=[d['id'] for d in context],canary_leaked=canary in answer['answer'],
                                       cited_reference=evidence_scores(case,[d for d in context if d['id'] in cited],5) if case.answerable else None)
                    row['status']='evaluated'
                except Exception as error:row.update(status='error',error_type=type(error).__name__)
                finally:row.update(calls=obs.calls,duration_ms=(perf_counter()-started)*1000);run_context.reset(token)
                write(path,row);return row
        async def monitored(case):
            row=await one(case);results.append(row)
            if len(results)%20==0:print(json.dumps({'phase':'B4_components','completed':len(results),'total':len(selected)}),flush=True)
        os.environ['PERF_CACHE_NAMESPACE']='quality-components'
        await asyncio.gather(*(monitored(c) for c in selected))
        assert await binding(db)==bound
        report={'completed':True,'binding':bound,'selected':len(results),'metrics':aggregate(results)}
        write(OUT/'component-summary.json',report);return report
    finally:await db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--concurrency',type=int,default=2)
    parser.add_argument('--output-dir',help='Use a new directory beneath .local for a fresh reproducibility run; existing bindings are never overwritten.')
    args=parser.parse_args()
    if args.output_dir:
        OUT=(PROJECT_ROOT/args.output_dir).resolve();assert OUT.is_relative_to(PROJECT_ROOT/'.local') and OUT!=PROJECT_ROOT/'.local'
    assert 1<=args.concurrency<=4;load_dotenv(ENV_PATH);asyncio.run(run(args.concurrency))
