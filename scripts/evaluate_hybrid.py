"""B2 retrieval-only ablation. Selection never reads test rankings or labels.

Fixed B1 public corpus/business date/embedding, no chat model or reranker calls.
"""
import argparse
import asyncio
from collections import defaultdict
from datetime import date,datetime,timezone
import hashlib
import itertools
import json
from pathlib import Path
import re
from statistics import mean
from time import perf_counter
from dotenv import load_dotenv
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.evaluation.v2_assets import validate,rows,ROOT
from smart_cs.evaluation.v2_schema import RagV2
from smart_cs.evaluation.runner import evidence_scores
from smart_cs.evaluation.v2_metrics import percentiles
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.retrieval.hybrid import HybridRetriever,HybridConfig,PROTOCOL,fuse
from smart_cs.retrieval.lexical import tokenizer,fingerprint,RESOURCES

OUT=PROJECT_ROOT/'.local/hybrid'
B1=PROJECT_ROOT/'.local/evaluation/v2-b1-20261116-resume'
DAY=date(2026,11,15)
GRID=[HybridConfig(k1=k1,b=b,dense_weight=weight,rrf_k=k,candidate_k=20) for k1,b,weight,k in itertools.product([1.2,1.5],[.5,.75],[.25,.5,.75],[20,60])]


def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def write(p,value):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);temporary=p.with_suffix('.writing');temporary.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8');temporary.replace(p)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def code():
    paths=[Path(__file__).resolve(),*[p for p in (PROJECT_ROOT/'src/smart_cs/retrieval').glob('*.py')],RESOURCES/'jieba_userdict_v1.txt',RESOURCES/'stopwords_v1.txt',PROJECT_ROOT/'requirements.lock.txt']
    return {p.relative_to(PROJECT_ROOT).as_posix():sha(p) for p in paths}
def lexical_cue(query):return bool(re.search(r'[A-Za-z][A-Za-z0-9_.-]{2,}|\d+|第[一二三四五六七八九十百]+条',query))


def score(case,documents):
    return {'id':case.id,'group_id':case.group_id,'split':case.split,'case_type':case.case_type,'answerable':case.answerable,'exact_cue':lexical_cue(case.message),
        'at_5':evidence_scores(case,documents,5) if case.answerable else None,'at_20':evidence_scores(case,documents,20) if case.answerable else None,
        'ids':[d['id'] for d in documents]}


def summary(values):
    positive=[r for r in values if r['answerable']];grouped=defaultdict(list)
    for r in positive:grouped[r['group_id']].append(r['at_5']['evidence_recall'])
    return {'cases':len(values),'answerable':len(positive),'groups':len(grouped),
        'recall_at_5':mean(r['at_5']['evidence_recall'] for r in positive) if positive else None,
        'recall_at_20':mean(r['at_20']['evidence_recall'] for r in positive) if positive else None,
        'hit_any_at_5':mean(r['at_5']['hit_any'] for r in positive) if positive else None,
        'all_evidence_at_5':mean(r['at_5']['all_groups'] for r in positive) if positive else None,
        'mrr_at_5':mean(r['at_5']['reciprocal_rank'] for r in positive) if positive else None,
        'mean_group_recall_at_5':mean(mean(v) for v in grouped.values()) if grouped else None,
        'failed_evidence_cases':[r['id'] for r in positive if not r['at_5']['all_groups']]}


async def bind(db):
    dataset=validate();old=read(B1/'run.json');dense=DenseRetriever(db,clock=lambda:DAY)
    if dataset!=old['dataset']:raise ValueError('Dataset differs from B1')
    # Existing B1 ranking, filters, embeddings and answer models must be unchanged.
    for name in ['retrieval/dense.py','retrieval/embedding.py','retrieval/clients.py','retrieval/policy.py','retrieval/grounding.py']:
        relative='src/smart_cs/'+name
        if sha(PROJECT_ROOT/relative)!=old['code_sha256'][relative]:raise ValueError('B1 comparison code drift: '+name)
    async with db.sessions() as s:index=await dense.active(s)
    if index.id!=old['index_id']:raise ValueError('Index changed')
    from smart_cs.evaluation.v2_assets import encoded
    if hashlib.sha256(encoded(index.configuration)).hexdigest()!=old['index_configuration_sha256']:raise ValueError('Index manifest changed')
    if await dense.adapter.aidentity()!=index.configuration['embedding_contract']:raise ValueError('Embedding identity changed')
    return {'dataset':dataset,'index_id':index.id,'index_configuration_sha256':old['index_configuration_sha256'],'embedding':index.model_identity,
        'business_date':DAY.isoformat(),'code_sha256':code(),'tokenizer':tokenizer().identity,'tokenizer_sha256':tokenizer().fingerprint,
        'bm25_algorithm':'BM25Plus(delta=0), positive log((N+1)/df) IDF; overlap required',
        'chat_model_calls':0,'reranker_calls':0,'rewrite':False,'labels_indexed':False,'human_gold':False,
        'exact_cue_rule':'Latin token >=3 chars, numeric token, or Chinese article-number pattern; fixed before test',
        'B1_original_run':B1.relative_to(PROJECT_ROOT).as_posix()}


def confirm_old(case,documents):
    original=read(B1/'retrieval'/(case.id+'.json'))
    if [d['id'] for d in documents[:20]]!=[d['id'] for d in original['documents']]:raise ValueError('Pure dense ranking differs from B1: '+case.id)


async def tune():
    if (OUT/'selection.json').exists():raise ValueError('Selection already frozen; choose a new experiment instead of tuning on test')
    db=configured_database()
    try:
        binding=await bind(db);write(OUT/'development-run.json',{'binding':binding,'grid':[c.model_dump() for c in GRID],
            'selection_rule':'validation group recall, then recall, MRR, conservative dense weight/defaults; enable only if recall gain>=.01, group recall not worse, hit not worse and MRR loss<=.02',
            'thresholds':{'minimum_recall_gain':.01,'maximum_mrr_loss':.02},'selected_splits':['train','validation'],'test_labels_used':False})
        selected=[r for r in rows() if isinstance(r,RagV2) and r.split in {'train','validation'} and r.evaluation_scope=='retrieval']
        hybrid=HybridRetriever(db,clock=lambda:DAY);records=[]
        for i,case in enumerate(selected):
            prepared=await hybrid.prepare(case.message);confirm_old(case,prepared.documents)
            values={}
            for cfg in GRID:
                found,sparse=prepared.rank(case.message,cfg,20);values[fingerprint(cfg.model_dump())]=score(case,found)
            record={'id':case.id,'split':case.split,'query':case.message,'dense':score(case,prepared.documents[:20]),'profiles':values,
                'lexical_snapshot_sha256':prepared.lexical.signature,'visible_chunks':len(prepared.documents),'timings_ms':prepared.timings}
            records.append(record);write(OUT/'development'/(case.id+'.json'),record)
            if (i+1)%20==0:print(json.dumps({'phase':'development','completed':i+1,'total':len(selected)}),flush=True)
        baseline=summary([r['dense'] for r in records if r['split']=='validation']);ranked=[]
        for cfg in GRID:
            key=fingerprint(cfg.model_dump());validation=summary([r['profiles'][key] for r in records if r['split']=='validation']);train=summary([r['profiles'][key] for r in records if r['split']=='train'])
            ranked.append({'config':cfg.model_dump(),'config_sha256':key,'validation':validation,'train':train})
        ranked.sort(key=lambda r:(r['validation']['mean_group_recall_at_5'],r['validation']['recall_at_5'],r['validation']['mrr_at_5'],r['config']['dense_weight'],r['config']['rrf_k']==60,r['config']['k1']==1.5,r['config']['b']==.75),reverse=True)
        winner=ranked[0];v=winner['validation'];enable=(v['recall_at_5']>=baseline['recall_at_5']+.01 and v['mean_group_recall_at_5']>=baseline['mean_group_recall_at_5'] and v['hit_any_at_5']>=baseline['hit_any_at_5'] and v['mrr_at_5']>=baseline['mrr_at_5']-.02)
        # Commit profile and decision BEFORE reading test results.
        write(RESOURCES/'hybrid_profile.json',winner['config'])
        selection={'frozen_at':datetime.now(timezone.utc).isoformat(),'binding':binding,'winner':winner,'dense_validation':baseline,'profiles':ranked,
            'recommended_online_mode':'hybrid' if enable else 'dense','decision_from_development_only':True,'test_labels_used':False,
            'profile_file_sha256':sha(RESOURCES/'hybrid_profile.json'),'reranker_enabled_in_B2':False}
        write(OUT/'selection.json',selection);print(json.dumps({k:selection[k] for k in ['winner','dense_validation','recommended_online_mode']},ensure_ascii=False,indent=2));return selection
    finally:await db.close()


async def compare():
    if (OUT/'comparison.json').exists():raise ValueError('Frozen comparison already exists; do not overwrite')
    selection=read(OUT/'selection.json');cfg=HybridConfig.model_validate(selection['winner']['config']);db=configured_database()
    try:
        binding=await bind(db)
        if binding!=selection['binding'] or sha(RESOURCES/'hybrid_profile.json')!=selection['profile_file_sha256']:raise ValueError('Selection inputs changed')
        dense=DenseRetriever(db,clock=lambda:DAY);hybrid=HybridRetriever(db,clock=lambda:DAY,config=cfg)
        selected=[r for r in rows() if isinstance(r,RagV2) and r.split=='test' and r.evaluation_scope=='retrieval'];test=[]
        for i,case in enumerate(selected):
            times={};results={}
            for mode in (['B1','B2'] if i%2==0 else ['B2','B1']):
                started=perf_counter();result=await (dense if mode=='B1' else hybrid).search(case.message,20);times[mode]=(perf_counter()-started)*1000;results[mode]=result
            confirm_old(case,results['B1']['documents'])
            found=results['B2']['documents'];sparse_ids={d['id'] for d in found if 'bm25' in d['ranks']}
            record={'id':case.id,'query':case.message,'B1':score(case,results['B1']['documents']),'B2':score(case,found),
                'B2_audit':results['B2']['retrieval'],'duration_ms':times,'bm25_contribution_in_fused_top20':sorted(sparse_ids)}
            test.append(record);write(OUT/'test'/(case.id+'.json'),record)
            if (i+1)%10==0:print(json.dumps({'phase':'frozen_test','completed':i+1,'total':len(selected)}),flush=True)
        development=[read(p) for p in sorted((OUT/'development').glob('*.json'))];key=selection['winner']['config_sha256']
        paired=[{'id':r['id'],'B1':r['dense'],'B2':r['profiles'][key]} for r in development]+test
        def pair_summary(items):return {'B1':summary([r['B1'] for r in items]),'B2':summary([r['B2'] for r in items])}
        positive=[r for r in paired if r['B1']['answerable']]
        improved=[r['id'] for r in positive if r['B2']['at_5']['evidence_recall']>r['B1']['at_5']['evidence_recall']]
        regressed=[r['id'] for r in positive if r['B2']['at_5']['evidence_recall']<r['B1']['at_5']['evidence_recall']]
        report={'completed':True,'binding':binding,'selection_sha256':sha(OUT/'selection.json'),'selected_config':cfg.model_dump(),
            'all_retrieval':pair_summary(paired),'by_split':{s:pair_summary([r for r in paired if r['B1']['split']==s]) for s in ['train','validation','test']},
            'exact_cue_slice':pair_summary([r for r in paired if r['B1']['exact_cue']]),'other_query_slice':pair_summary([r for r in paired if not r['B1']['exact_cue']]),
            'test_exact_cue_slice':pair_summary([r for r in test if r['B1']['exact_cue']]),
            'runtime_latency_test':{'cases':len(test),'ordering':'alternating B1/B2 per query; same process; warm tokenizer; per-request corpus build included',
                'B1_ms':percentiles([r['duration_ms']['B1'] for r in test]),'B2_ms':percentiles([r['duration_ms']['B2'] for r in test]),
                'paired_added_ms':percentiles([r['duration_ms']['B2']-r['duration_ms']['B1'] for r in test])},
            'improved_cases':improved,'regressed_cases':regressed,'pure_dense_rankings_match_original_B1':True,
            'deployment_decision':selection['recommended_online_mode'],'test_used_to_select_parameters':False,
            'limitations':['Candidate labels pending human review.','Retrieval component only; no B2 generated-answer accuracy measured.','Negative queries not scored as refusal.','Cold tokenizer initialization excluded from warm-query timing.','Application-side BM25 snapshot rebuilt per request; intended for current small corpus.']}
        if await bind(db)!=binding:raise ValueError('Comparison inputs drifted')
        write(OUT/'comparison.json',report);return report
    finally:await db.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['tune','compare']);args=p.parse_args();load_dotenv(ENV_PATH);OUT.mkdir(exist_ok=True)
    result=asyncio.run(tune() if args.action=='tune' else compare())
    print(json.dumps({'completed':True,'output':str(OUT),'deployment_decision':result.get('deployment_decision',result.get('recommended_online_mode'))},ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
