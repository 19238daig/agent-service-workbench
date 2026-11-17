import asyncio
import json
import os
from pathlib import Path
from statistics import mean
from time import perf_counter
import tempfile
from smart_cs.evaluation.assets import ROOT,CORPUS,records,validate,normalize
from smart_cs.evaluation.schema import RagCase,IntentCase,ComplianceCase,TicketCase
from smart_cs.settings import PROJECT_ROOT


def evidence_scores(case,documents,k):
    ranks=[];found=0
    for group in case.evidence:
        matches=[i+1 for i,doc in enumerate(documents[:k]) if any(
            (doc['metadata']['document_id']==ref.document_id or doc['metadata'].get('artifact_sha256')==ref.source_sha256) and
            (ref.page is None or doc['metadata'].get('page')==ref.page) and
            normalize(ref.quote) in normalize(doc['content']) for ref in group.alternatives)]
        if matches:found+=1;ranks.extend(matches)
    return {'hit_any':bool(found),'all_groups':found==len(case.evidence),
            'evidence_recall':found/len(case.evidence),'reciprocal_rank':1/min(ranks) if ranks else 0.0}


async def ticket_probe(case):
    if case.capability_status=='planned':return {'id':case.id,'status':'not_run','reason':'planned capability'}
    if case.expected_operation=='clarify':return {'id':case.id,'status':'not_run','reason':'requires Agent slot-filling evaluation; repository cannot classify intent'}
    from decimal import Decimal
    from sqlalchemy import select,func
    from smart_cs.db.database import Database
    from smart_cs.db.models import User,Order,Ticket,utcnow
    from smart_cs.db.tickets import DatabaseTicketStore
    with tempfile.TemporaryDirectory() as directory:
        database=Database('sqlite+aiosqlite:///'+str(Path(directory)/'case.db').replace('\\','/'))
        try:
            await database.initialize(create_schema=True)
            async with database.sessions.begin() as session:
                session.add_all([User(id=u,display_name='合成评测用户',role='customer',source='evaluation_fixture') for u in ['a','b']]);await session.flush()
                session.add(Order(id='eval-order',user_id=case.fixture.get('order_owner','a'),product_name='合成评测商品',paid_amount=Decimal('99'),currency='CNY',status='delivered',paid_at=utcnow(),source='evaluation_fixture'))
            store=DatabaseTicketStore(database);fixture=case.fixture;ticket=None
            if case.id in {'ticket-query','ticket-foreign-ticket','ticket-transition'}:
                ticket=await store.create('general','medium','已有合成工单','评测用',fixture.get('ticket_owner','a'))
                if case.id=='ticket-transition':await store.update_status(ticket['ticket_id'],'closed','a')
            async with database.sessions() as session:before=await session.scalar(select(func.count()).select_from(Ticket))
            rejected=False
            try:
                if case.id=='ticket-transition':await store.update_status(ticket['ticket_id'],'processing','a')
                elif case.id in {'ticket-query','ticket-foreign-ticket','ticket-missing-ticket'}:
                    result=await store.query(ticket['ticket_id'] if ticket else 'missing','a');rejected=result is None
                else:
                    order='missing' if fixture.get('order_exists') is False else 'eval-order' if 'order_owner' in fixture else None
                    await store.create('refund','medium','合成评测申请','仅登记申请，不执行支付','a',order_id=order)
            except ValueError:rejected=True
            async with database.sessions() as session:after=await session.scalar(select(func.count()).select_from(Ticket))
            passed=(after-before)==case.expected_effects['new_tickets'] and rejected==(case.expected_operation=='reject')
            return {'id':case.id,'status':'passed' if passed else 'failed','scope':'isolated SQLite repository only',
                    'new_tickets':after-before,'rejected':rejected,'not_tested':['live Agent planning','authentication','approvals','payment']}
        finally:await database.close()


async def evaluate(split='test',live_models=False,root=ROOT,corpus=CORPUS,output=None):
    from smart_cs.db.database import configured_database
    from smart_cs.memory.bge_memory import BGEKnowledgeMemory
    from smart_cs.db.models import RetrievalIndex
    from sqlalchemy import select
    validation=validate(root,corpus)
    database=configured_database()
    if database is None:raise RuntimeError('Configured PostgreSQL required for retrieval evaluation')
    selected=[r for r in records(root) if r.split==split]
    manifest=json.loads((Path(root)/'manifest.json').read_text(encoding='utf-8'))
    output=Path(output or PROJECT_ROOT/f'.local/evaluation/{manifest["id"]}-{split}.json')
    if output.exists():
        await database.close()
        raise ValueError('Evaluation report exists; choose a new --output before running models again')
    report={'dataset':validation,'split':split,'human_reviewed':False,'benchmark_status':'candidate_diagnostic_not_gold',
        'threshold_tuned_on_this_run':False,'configuration':{'candidate_k':20,'rerank_k':5,'online_context_k':3,
        'rerank_min_score':float(os.getenv('RAG_RERANK_MIN_SCORE','.10'))},'results':[]}
    models=None
    if live_models:
        from smart_cs.models import build_agent_models
        models=build_agent_models()
        report['model_aliases']={role:str(getattr(model,'model_name',type(model).__name__)) for role,model in models.items()}
    try:
        memory=BGEKnowledgeMemory(database);report['index']=await memory.validate_runtime()
        async with database.sessions() as session:
            index=await session.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
            if index.configuration['source_manifest_hash']!=manifest['source_manifest_sha256']:raise ValueError('Active index does not match frozen evaluation corpus')
        for case in selected:
            started=perf_counter();row={'id':case.id,'task':case.task}
            try:
                if isinstance(case,RagCase):
                    if case.evaluation_scope=='generation_only':
                        row.update(status='not_run',reason='Needs controlled generation and independent answer/injection review, not a retrieval metric')
                    else:
                        raw=await memory.search(case.message,20);ranked=await memory.rerank(case.message,raw,top_k=5)
                        row['retrieved']=[{'id':d['id'],'source':d['source'],'page':d['metadata']['page'],'heading':d['metadata']['heading'],'score':d['rerank_score']} for d in ranked]
                        if case.answerable:
                            row.update(status='evaluated',vector_at_5=evidence_scores(case,raw,5),rerank_at_5=evidence_scores(case,ranked,5),rerank_at_3=evidence_scores(case,ranked,3))
                        else:row.update(status='evaluated',unsupported_query_filtered=not ranked)
                elif isinstance(case,TicketCase):row.update(await ticket_probe(case))
                elif models is None:row.update(status='not_run',reason='Pass --live-models for real provider evaluation; no fake scores')
                elif isinstance(case,IntentCase):
                    from langchain_core.messages import HumanMessage,AIMessage
                    from smart_cs.agents.intent_router import IntentRouterAgent
                    history=[(HumanMessage if m['role']=='user' else AIMessage)(content=m['content']) for m in case.history]
                    value=await IntentRouterAgent(models['intent_router']).classify(case.message,history)
                    route='clarification' if value.primary_intent.value=='unknown' or value.confidence<.5 else 'compliance_checker' if value.primary_intent.value=='compliance' else value.suggested_agent
                    row.update(status='evaluated',expected=case.expected_route,predicted=route,correct=route==case.expected_route,confidence=value.confidence)
                elif isinstance(case,ComplianceCase):
                    from smart_cs.agents.compliance_checker import ComplianceCheckerAgent
                    value=await ComplianceCheckerAgent(models['compliance_checker']).full_check(case.candidate_answer)
                    decision='allow' if value.passed else 'block'
                    row.update(status='evaluated',expected=case.expected_decision,predicted=decision,correct=decision==case.expected_decision)
            except Exception as error:row.update(status='error',error_type=type(error).__name__)
            row['duration_ms']=round((perf_counter()-started)*1000,2);report['results'].append(row)
            print(f"{case.id}: {row['status']}",flush=True)
        positives=[r for r in report['results'] if 'rerank_at_5' in r]
        negatives=[r for r in report['results'] if 'unsupported_query_filtered' in r]
        report['metrics']={'retrieval_answerable_cases':len(positives),'unsupported_cases':len(negatives)}
        if positives:
            for mode in ['vector_at_5','rerank_at_5','rerank_at_3']:
                report['metrics'][mode]={key:mean(float(r[mode][key]) for r in positives) for key in positives[0][mode]}
        if negatives:report['metrics']['unsupported_filter_rate']=mean(r['unsupported_query_filtered'] for r in negatives)
        for task in ['intent','compliance']:
            cases=[r for r in report['results'] if r['task']==task and r['status']=='evaluated']
            if cases:report['metrics'][task]={'samples':len(cases),'accuracy':mean(r['correct'] for r in cases)}
        report['errors']=sum(r['status']=='error' for r in report['results'])
        report['limitations']=['AI-authored candidate labels; 0 independently human-approved gold cases at release.',
            'Exact normalized source-quote locator matching, not semantic answer correctness or full citation support.',
            'Unsupported-query filtering is not end-to-end refusal accuracy.',
            'Generation, document injection and version reasoning cases are not scored by this retrieval runner.',
            'Ticket results exercise an isolated SQLite repository, not authenticated production workflows.',
            'No claim of reaching roadmap quality targets; retain failed, error and not-run cases.']
    finally:await database.close()
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'metrics':report['metrics'],'errors':report['errors'],'report':str(output)},ensure_ascii=False,indent=2))
    return report
