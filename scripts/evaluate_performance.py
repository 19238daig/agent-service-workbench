"""B4: validation-only pilots, then fixed public evidence/cache and rewrite tests."""
import argparse,asyncio,hashlib,json,os
from datetime import date,datetime,timezone
from pathlib import Path
from statistics import mean
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage,AIMessage,SystemMessage
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.models import build_agent_models
from smart_cs.evaluation.v2_assets import rows,validate,encoded
from smart_cs.evaluation.v2_schema import RagV2,IntentV2,ComplianceV2
from smart_cs.evaluation.v2_runner import intent_score,valid_intent_format
from smart_cs.evaluation.runner import evidence_scores
from smart_cs.evaluation.v2_metrics import percentiles
from smart_cs.memory.bge_memory import BGEKnowledgeMemory
from smart_cs.agents.knowledge_rag import KnowledgeRAGAgent
from smart_cs.agents.intent_router import IntentRouterAgent
from smart_cs.agents.action_planner import PLANNER_PROMPT
from smart_cs.schemas.actions import ActionPlan,decode_plan
from smart_cs.services.compliance import ComplianceReviewService
from smart_cs.retrieval.grounding import GroundedAnswerService
from smart_cs.retrieval.reranking import RerankConfig
from smart_cs.observability.models import invoke_model
from smart_cs.observability.context import RunObservation,run_context,usage_summary
from smart_cs.performance.policy import PROFILE,ServingPolicy
from smart_cs.performance.rewrite import needs_rewrite
from smart_cs.performance.runtime import summary as performance_summary
from evaluate_hybrid import read,write,sha

OUT=PROJECT_ROOT/'.local/performance';DAY=date(2026,11,15)


def sources():
    paths=[Path(__file__).resolve(),PROJECT_ROOT/'src/smart_cs/models.py',PROJECT_ROOT/'src/smart_cs/memory/bge_memory.py',
           PROJECT_ROOT/'src/smart_cs/agents/knowledge_rag.py',PROJECT_ROOT/'src/smart_cs/services/chat.py',
           PROJECT_ROOT/'src/smart_cs/persistence/conversations.py',PROJECT_ROOT/'src/smart_cs/durable/executor.py',
           *sorted((PROJECT_ROOT/'src/smart_cs/performance').glob('*.py')),
           *sorted((PROJECT_ROOT/'src/smart_cs/observability').glob('*.py'))]
    return {p.relative_to(PROJECT_ROOT).as_posix():sha(p) for p in paths}


async def bind(db):
    old=read(PROJECT_ROOT/'.local/reranker/selection.json');dataset=validate()
    assert dataset==old['binding']['dataset']
    for name in ['dense.py','embedding.py','clients.py','policy.py','grounding.py','reranking.py','hybrid.py','lexical.py']:
        relative='src/smart_cs/retrieval/'+name
        assert sha(PROJECT_ROOT/relative)==old['binding']['code_sha256'][relative]
    memory=BGEKnowledgeMemory(db,clock=lambda:DAY)
    async with db.sessions() as s:index=await memory.retriever().active(s)
    assert index.id==old['binding']['index_id']
    assert hashlib.sha256(encoded(index.configuration)).hexdigest()==old['binding']['index_configuration_sha256']
    assert await memory.retriever().adapter.aidentity()==index.configuration['embedding_contract']
    return {'dataset':dataset,'index':index.id,'date':DAY.isoformat(),'B3_selection_sha256':sha(PROJECT_ROOT/'.local/reranker/selection.json'),
            'B3_comparison_sha256':sha(PROJECT_ROOT/'.local/reranker/comparison.json'),'rerank_profile':RerankConfig.deployed().model_dump(),
            'code_sha256':sources(),'human_gold':False,'generation_quality_scope':'small model pilot only; no production accuracy'}


def select_groups(values,limit):
    selected=[];seen=set()
    for r in sorted(values,key=lambda x:x.id):
        if r.group_id in seen:continue
        seen.add(r.group_id);selected.append(r)
        if len(selected)>=limit:break
    return selected


async def evidence(db,query,enabled):
    with patch.dict(os.environ,{'PERF_ENABLED':'true' if enabled else 'false'}):
        memory=BGEKnowledgeMemory(db,clock=lambda:DAY)
        candidates=await memory.search(query,memory.candidate_k)
        documents=await memory.rerank(query,candidates,memory.context_k)
        return candidates,documents


async def tune():
    assert not (OUT/'selection.json').exists(),'Selection already frozen'
    db=configured_database();models=build_agent_models();binding=await bind(db)
    protocol={'binding':binding,'test_labels_used':False,'model_gate':'pilot diagnostic only; no model auto-switch with <10 independent groups per role',
              'rewrite_gate':'retain on-demand only if validation context recall does not decrease by more than .01',
              'cache_gate':'canonical IDs/content/metadata and final evidence must match; invalid signatures/ACL/model drift never served',
              'token_budget':'observed reported tokens, plus output caps and call count; unknown is never zero',
              'ticket_scope':'8 synthetic development proposal-only contracts; no business writes'}
    write(OUT/'development-run.json',protocol)
    pilot=[]
    try:
        all_rows=rows()
        intent=select_groups([r for r in all_rows if isinstance(r,IntentV2) and r.split=='validation'],8)
        rag=select_groups([r for r in all_rows if isinstance(r,RagV2) and r.split=='validation' and r.evaluation_scope=='retrieval'],8)
        compliance=select_groups([r for r in all_rows if isinstance(r,ComplianceV2) and r.split=='validation'],8)
        ticket=[('只查询订单 ORDER-A-001 的状态，不办理退款。','order_query'),('核验订单 ORDER-A-001 的退款申请条件。','order_eligibility'),
                ('查询工单 TK-20261114-0001 的状态。','ticket_query'),('列出我本人的工单。','ticket_list'),
                ('查看工单 TK-20261114-0001 的流转记录。','ticket_events'),('为订单 ORDER-A-001 草拟退款工单，申请20.00元，原因是商品损坏。','ticket_create'),
                ('为订单 ORDER-A-001 草拟投诉工单，摘要为物流延误。','ticket_create'),('把工单 TK-20261114-0001 的状态草拟为 processing，版本1，原因是受理核实。','ticket_transition')]
        tasks={'intent_router':intent,'knowledge_rag':rag,'compliance_checker':compliance,'ticket_handler':list(enumerate(ticket))}
        alternatives={'intent_router':'knowledge_rag','knowledge_rag':'compliance_checker','ticket_handler':'knowledge_rag','compliance_checker':'intent_router'}
        for role,cases in tasks.items():
            for item in cases:
                identifier=item.id if not isinstance(item,tuple) else 'development-ticket-'+str(item[0])
                contexts=None
                if role=='knowledge_rag':_,contexts=await evidence(db,item.message,True)
                for source in [role,alternatives[role]]:
                    obs=RunObservation(uuid4().hex,'model-pilot','model-pilot');token=run_context.set(obs);started=perf_counter()
                    record={'role':role,'source_role':source,'model':models[source].model_name,'id':identifier,'status':'started','correct':False}
                    path=OUT/'model-pilot'/(role+'-'+identifier+'-'+source+'.json');write(path,record)
                    try:
                        if role=='intent_router':
                            value=await IntentRouterAgent(models[source]).classify(item.message)
                            scored=intent_score(item,value);record.update(correct=scored['joint_correct'],result=scored)
                        elif role=='knowledge_rag':
                            result=await GroundedAnswerService(models[source],revalidate=BGEKnowledgeMemory(db,clock=lambda:DAY).revalidate,clock=lambda:DAY).answer(item.message,contexts)
                            cited={c['chunk_id'] for c in result['citations']}
                            score=evidence_scores(item,[d for d in contexts if d['id'] in cited],5) if item.answerable else None
                            record.update(correct=(result['status']=='answered' and score['all_groups']) if item.answerable else result['status']=='clarify' and result['reason_code']!='invalid_output',
                                          result=result,cited_reference=score)
                        elif role=='compliance_checker':
                            result=(await ComplianceReviewService(models[source]).review_content(item.candidate_answer,item.state)).model_dump(mode='json')
                            record.update(correct=result['decision']==item.expected_decision and result['reviewer']!='fail_closed',result=result)
                        else:
                            text,expected=item[1]
                            response=await invoke_model(models[source],[SystemMessage(content=PLANNER_PROMPT+'\nSchema:'+json.dumps(ActionPlan.model_json_schema())),HumanMessage(content=text)])
                            plan=decode_plan(response.content);record.update(correct=plan.action.name==expected,result=plan.model_dump(),expected=expected)
                        record['status']='evaluated'
                    except Exception as e:record.update(status='error',error_type=type(e).__name__)
                    finally:
                        record.update(duration_ms=(perf_counter()-started)*1000,calls=obs.calls,usage=usage_summary(obs.calls));run_context.reset(token)
                    write(path,record);pilot.append(record)
            print(json.dumps({'phase':'model_pilot','role':role,'records':sum(r['role']==role for r in pilot)}),flush=True)
        rewrite=[]
        for case in rag:
            history=[HumanMessage(content='你好'),AIMessage(content='请说明你的问题。')]
            obs=RunObservation(uuid4().hex,'rewrite-pilot','rewrite-pilot');token=run_context.set(obs)
            try:
                rewritten=await KnowledgeRAGAgent(models['knowledge_rag']).rewrite_query(case.message,history)
                _,old=await evidence(db,rewritten,False);_,new=await evidence(db,case.message,True)
                before=evidence_scores(case,old,5) if case.answerable else None;after=evidence_scores(case,new,5) if case.answerable else None
                rewrite.append({'id':case.id,'answerable':case.answerable,'before':before,'after':after,'rewritten_query':rewritten,
                                'would_skip':not needs_rewrite(case.message,history),'calls':obs.calls})
            finally:run_context.reset(token)
        positives=[r for r in rewrite if r['answerable'] and r['would_skip']]
        preserve=not positives or mean(r['after']['evidence_recall'] for r in positives)>=mean(r['before']['evidence_recall'] for r in positives)-.01
        profile=ServingPolicy.deployed().model_dump();profile['rewrite_on_demand']=preserve;profile['role_sources']={}
        write(PROFILE,profile)
        selection={'binding':binding,'frozen_at':datetime.now(timezone.utc).isoformat(),'test_labels_used':False,'profile':profile,
                   'profile_sha256':sha(PROFILE),'role_sources_unchanged':True,'model_pilot_records':len(pilot),
                   'model_pilot_summary':{role:{source:{'samples':len([r for r in pilot if r['role']==role and r['source_role']==source]),
                       'candidate_correct':sum(r['correct'] for r in pilot if r['role']==role and r['source_role']==source),
                       'errors':sum(r['status']=='error' for r in pilot if r['role']==role and r['source_role']==source),
                       'latency_ms':percentiles([r['duration_ms'] for r in pilot if r['role']==role and r['source_role']==source])}
                       for source in [role,alternatives[role]]} for role in tasks},
                   'rewrite_validation':rewrite,'rewrite_on_demand':preserve,'retained_models_reason':'Small role pilots lack 10 independent groups; no automatic replacement, especially safety reviewers.'}
        assert await bind(db)==binding
        write(OUT/'selection.json',selection)
        print(json.dumps({'parameters_frozen':True,'rewrite_enabled':preserve,'model_pilot_records':len(pilot)}),flush=True)
    finally:await db.close()


async def compare():
    assert not (OUT/'comparison.json').exists(),'Frozen comparison exists'
    selection=read(OUT/'selection.json');db=configured_database();records=[]
    try:
        assert await bind(db)==selection['binding'] and sha(PROFILE)==selection['profile_sha256']
        os.environ['PERF_CACHE_NAMESPACE']='evaluation-'+uuid4().hex
        cases=[r for r in rows() if isinstance(r,RagV2) and r.split=='test' and r.evaluation_scope=='retrieval']
        for i,case in enumerate(cases):
            record={'id':case.id,'answerable':case.answerable};values={}
            for mode in (['B3','B4_cold','B4_warm'] if i%2==0 else ['B4_cold','B4_warm','B3']):
                obs=RunObservation(uuid4().hex,'retrieval-compare','retrieval-compare');token=run_context.set(obs);start=perf_counter()
                try:candidates,context=await evidence(db,case.message,mode!='B3')
                finally:run_context.reset(token)
                values[mode]=(candidates,context)
                record[mode]={'duration_ms':(perf_counter()-start)*1000,'candidate_ids':[d['id'] for d in candidates],
                              'context_ids':[d['id'] for d in context],'calls':obs.calls,'performance':performance_summary(obs),
                              'context_score':evidence_scores(case,context,5) if case.answerable else None}
            for mode in ['B4_cold','B4_warm']:
                for before,after in zip(values['B3'],values[mode]):
                    assert [(d['id'],d['content'],d['metadata']) for d in before]==[(d['id'],d['content'],d['metadata']) for d in after]
            old=read(PROJECT_ROOT/'.local/reranker/test'/(case.id+'.json'))['online_dense']
            assert record['B3']['context_ids']==old['context_ids']
            write(OUT/'test'/(case.id+'.json'),record);records.append(record)
            if (i+1)%8==0:print(json.dumps({'phase':'frozen_cache_test','completed':i+1,'total':len(cases)}),flush=True)
        result={'completed':True,'binding':selection['binding'],'selection_sha256':sha(OUT/'selection.json'),'test_used_for_selection':False,
                'cases':len(records),'all_canonical_evidence_equal':True,'B3_matches_step24':True,
                'metrics':{mode:{'latency_ms':percentiles([r[mode]['duration_ms'] for r in records]),
                    'context_recall':mean(r[mode]['context_score']['evidence_recall'] for r in records if r['answerable']),
                    'local_model_attempts':sum(len(r[mode]['calls']) for r in records),
                    'cache_hits':sum(r[mode]['performance']['cache_hits'] for r in records),
                    'cache_errors':sum(r[mode]['performance']['cache_errors'] for r in records)} for mode in ['B3','B4_cold','B4_warm']},
                'limitations':['Candidate labels pending human review.','Retrieval component: no generated answers in this comparison.',
                               'Repeated identical public questions measure warm-cache behavior, not production hit rate.',
                               'Sequential alternating order; warm local models, no production throughput claim.']}
        assert await bind(db)==selection['binding'];write(OUT/'comparison.json',result)
    finally:await db.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['tune','compare']);args=p.parse_args();load_dotenv(ENV_PATH)
    asyncio.run(tune() if args.action=='tune' else compare())


if __name__=='__main__':main()
