"""Frozen development-label diagnostic for exact dense retrieval, no reranker/LLM.

Uses existing source-anchored candidate labels. They are NOT independently
human-reviewed gold; no labels/questions are imported into the knowledge index.
"""
import argparse
import asyncio
import hashlib
import json
from statistics import mean
from time import perf_counter
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import select
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import RetrievalIndex
from smart_cs.evaluation.assets import ROOT,CORPUS,records,validate
from smart_cs.evaluation.schema import RagCase
from smart_cs.evaluation.runner import evidence_scores
from smart_cs.retrieval.dense import DenseRetriever


async def evaluate(split,output):
    validation=validate(ROOT,CORPUS)
    label_manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
    db=configured_database();report={'passed':False,'split':split,'kind':'exact_dense_development_diagnostic',
        'human_reviewed':False,'gold_standard':False,'chat_model_calls':0,'reranker_calls':0,
        'questions_or_labels_indexed':False,'query_or_threshold_tuned_on_this_run':False,'results':[]}
    try:
        retriever=DenseRetriever(db)
        async with db.sessions() as s:index=await retriever.active(s)
        if index.configuration.get('source_manifest_hash')!=label_manifest['source_manifest_sha256']:
            raise ValueError('Public source corpus differs from the frozen labels')
        report.update(index_id=index.id,embedding=index.model_identity['embedding'],source_manifest_sha256=label_manifest['source_manifest_sha256'],dataset=validation)
        for case in [r for r in records(ROOT) if isinstance(r,RagCase) and r.split==split]:
            row={'id':case.id,'case_type':case.case_type,'question':case.message,'answerable':case.answerable}
            if case.evaluation_scope!='retrieval':row.update(status='not_run',reason='generation_only')
            else:
                started=perf_counter()
                try:
                    result=await retriever.search(case.message,20)
                    docs=result['documents']
                    row.update(status='evaluated',duration_ms=round((perf_counter()-started)*1000,2),retrieved=[
                        {'id':d['id'],'document_id':d['metadata']['document_id'],'source':d['source'],'page':d['metadata']['page'],'heading':d['metadata']['heading'],'cosine_similarity':d['score']} for d in docs[:5]])
                    if case.answerable:row.update(at_5=evidence_scores(case,docs,5),at_20=evidence_scores(case,docs,20))
                    else:row.update(refusal_not_scored=True,reason='Dense nearest-neighbour search always returns nearest candidates; rejection is step thirteen')
                except Exception as error:row.update(status='error',error_type=type(error).__name__)
            report['results'].append(row)
        scored=[r for r in report['results'] if 'at_5' in r]
        errors=[r['id'] for r in report['results'] if r['status']=='error']
        report['summary']={'total_cases':len(report['results']),'answerable_retrieval_cases':len(scored),
            'hit_any_at_5':sum(r['at_5']['hit_any'] for r in scored),
            'mean_evidence_recall_at_5':mean(r['at_5']['evidence_recall'] for r in scored) if scored else None,
            'mean_evidence_recall_at_20':mean(r['at_20']['evidence_recall'] for r in scored) if scored else None,
            'mrr_at_5':mean(r['at_5']['reciprocal_rank'] for r in scored) if scored else None,
            'failed_evidence_cases':[r['id'] for r in scored if not r['at_5']['all_groups']],
            'errors':errors}
        report['passed']=bool(scored) and not errors
        report['pass_meaning']='Diagnostic completed without execution errors; not a quality/gold-label acceptance claim'
    finally:
        await db.close()
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--split',choices=['train','validation','test'],default='train')
    parser.add_argument('--output',default='.local/retrieval/dense-development-evaluation.json');args=parser.parse_args();load_dotenv(ENV_PATH)
    output=(PROJECT_ROOT/args.output).resolve()
    if not output.is_relative_to(PROJECT_ROOT/'.local/retrieval') or output.exists():parser.error('Choose a NEW report path beneath .local/retrieval/')
    try:
        report=asyncio.run(evaluate(args.split,output));print(json.dumps({'passed':report['passed'],'summary':report['summary'],'human_reviewed':False},indent=2));return 0 if report['passed'] else 1
    except Exception as error:print(json.dumps({'passed':False,'error_type':type(error).__name__}));return 1


if __name__=='__main__':raise SystemExit(main())
