"""B1 RAG baseline: exact dense top-5 -> anchored answers (no rewrite/rerank).

Frozen candidate labels are never added to the index. Generation-only injection
cases add their attack string as an explicit untrusted field, never as evidence.
This is the RAG portion of B1, not a benchmark of write/approval workflows.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from statistics import mean
from time import perf_counter
from dotenv import load_dotenv
from smart_cs.db.database import configured_database
from smart_cs.evaluation.assets import ROOT,CORPUS,records,validate
from smart_cs.evaluation.schema import RagCase
from smart_cs.evaluation.runner import evidence_scores
from smart_cs.models import build_agent_models
from smart_cs.observability.context import RunObservation,run_context,usage_summary
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.retrieval.grounding import GroundedAnswerService,policy_fingerprint
from smart_cs.settings import PROJECT_ROOT,ENV_PATH


def file_hash(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def summary(rows):
    evaluated=[r for r in rows if r['status']=='evaluated']
    positives=[r for r in evaluated if r['answerable'] and r['evaluation_scope']=='retrieval']
    negatives=[r for r in evaluated if not r['answerable'] and r['evaluation_scope']=='retrieval']
    released=[r for r in evaluated if r['result']['status']=='answered']
    quotes=[q for r in released for c in r['result']['citations'] for q in c['quotes']]
    return {'total':len(rows),'evaluated':len(evaluated),'errors':[r['id'] for r in rows if r['status']=='error'],
        'answerable_retrieval_cases':len(positives),
        'dense_evidence_recall_at_5':mean(r['retrieval_at_5']['evidence_recall'] for r in positives) if positives else None,
        'dense_mrr_at_5':mean(r['retrieval_at_5']['reciprocal_rank'] for r in positives) if positives else None,
        'released_answerable_cases':sum(r['result']['status']=='answered' for r in positives),
        'answerable_clarification_rate':mean(r['result']['status']!='answered' for r in positives) if positives else None,
        'unanswerable_cases':len(negatives),
        'candidate_correct_abstention_rate':mean(r['result']['status']!='answered' for r in negatives) if negatives else None,
        'released_answers':len(released),'released_claims':sum(len(r['result']['claims']) for r in released),
        'literal_quote_anchors':len(quotes),
        'generation_only_controls':sum(r['evaluation_scope']=='generation_only' for r in rows),
        'needs_review_or_failed_cases':[r['id'] for r in evaluated if
            (r['answerable'] and r['evaluation_scope']=='retrieval' and (r['result']['status']!='answered' or not r['cited_reference_at_5']['all_groups'])) or
            (not r['answerable'] and r['result']['status']=='answered') or r['evaluation_scope']=='generation_only'],
        'human_answer_correctness':None,'independent_citation_support_rate':None}


async def evaluate(split,folder):
    dataset=validate(ROOT,CORPUS)
    selected=[c for c in records(ROOT) if isinstance(c,RagCase) and (split=='all' or c.split==split)]
    manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
    db=configured_database()
    if db is None:raise ValueError('Configured PostgreSQL required')
    observation=RunObservation('b1-offline','b1-offline','b1-offline');token=run_context.set(observation)
    report={'passed':False,'kind':'B1_RAG_first_baseline','split':split,'human_reviewed':False,'gold_standard':False,
        'query_or_threshold_tuned_on_this_run':False,'configuration':{'retrieval':'exact_cosine','candidate_k':20,'context_k':5,
            'rewrite':False,'reranker':False,'bm25':False,'authenticated_scope':'public_only','support_review':'same configured knowledge model, separate call'},
        'dataset':dataset,'source_manifest_sha256':manifest['source_manifest_sha256'],'policy_sha256':policy_fingerprint(),
        'source_code_sha256':{str(p.relative_to(PROJECT_ROOT)):file_hash(p) for p in [
            PROJECT_ROOT/'src/smart_cs/retrieval/grounding.py',PROJECT_ROOT/'src/smart_cs/schemas/grounding.py',
            PROJECT_ROOT/'src/smart_cs/retrieval/dense.py',Path(__file__).resolve()]},'results':[]}
    models=build_agent_models();llm=models['knowledge_rag'];report['knowledge_model']=str(getattr(llm,'model_name',type(llm).__name__))
    try:
        retriever=DenseRetriever(db)
        async with db.sessions() as session:index=await retriever.active(session)
        if index.configuration.get('source_manifest_hash')!=manifest['source_manifest_sha256']:raise ValueError('Frozen source corpus mismatch')
        report.update(index_id=index.id,embedding=index.model_identity['embedding'])
        service=GroundedAnswerService(llm,revalidate=retriever.revalidate)
        for case in selected:
            start=perf_counter();before=len(observation.calls)
            row={'id':case.id,'split':case.split,'question':case.message,'case_type':case.case_type,'answerable':case.answerable,
                'evaluation_scope':case.evaluation_scope,'reference_points':case.reference_points,
                'forbidden_claims':case.forbidden_claims,'human_review':'pending'}
            try:
                response=await retriever.search(case.message,20);docs=response['documents'];context=docs[:5]
                if case.untrusted_context and context:
                    context=[{**context[0],'untrusted_instruction':case.untrusted_context},*context[1:]]
                    row['controlled_attack']=case.untrusted_context
                    row['attack_added_to_database']=False
                audit={}
                result=await service.answer(case.message,context,audit=audit)
                if result['citations']:await retriever.revalidate_citations(result['citations'])
                cited_ids={c['chunk_id'] for c in result['citations']}
                cited=[d for d in context if d['id'] in cited_ids]
                row.update(status='evaluated',result=result,structured_audit=audit,
                    retrieved=[{'id':d['id'],'content':d['content'],'source':d['source'],'metadata':d['metadata'],'cosine':d['score']} for d in context])
                if case.answerable:
                    row.update(retrieval_at_5=evidence_scores(case,docs,5),retrieval_at_20=evidence_scores(case,docs,20),
                        cited_reference_at_5=evidence_scores(case,cited,5))
            except Exception as error:
                row.update(status='error',error_type=type(error).__name__)
            row['duration_ms']=round((perf_counter()-start)*1000,2)
            row['calls']=[{k:c.get(k) for k in ['kind','model','operation','status','error_code','duration_ms','usage']} for c in observation.calls[before:]]
            report['results'].append(row)
            (folder/'report.partial.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'case':case.id,'status':row['status'],'answer_status':row.get('result',{}).get('status'),'reason':row.get('result',{}).get('reason_code')},ensure_ascii=False),flush=True)
        report['summary']=summary(report['results'])
        report['by_split']={s:summary([r for r in report['results'] if r['split']==s]) for s in sorted({r['split'] for r in report['results']})}
        report['usage']=usage_summary(observation.calls)
        report['model_calls']=len(observation.calls)
        report['frozen_assets_still_valid']=validate(ROOT,CORPUS)==dataset
        report['passed']=bool(report['results']) and not report['summary']['errors'] and report['frozen_assets_still_valid']
        report['pass_meaning']='Execution and provenance checks completed; not a quality target or production accuracy claim'
        report['limitations']=[
            'AI-authored source-grounded candidate labels, no independently human-reviewed gold.',
            'B1 RAG component only; no ticket writes, approvals or single-agent comparison measured.',
            'No rewrite/rerank in this baseline; online chat retains its existing local reranker stage.',
            'Literal quote matching plus a separate same-model review is fallible; true semantic citation support and answer correctness await independent review.',
            'Generation-only version/injection cases use the same dense retrieval, with attacks in a controlled untrusted field, not a production security guarantee.',
            'Mean evidence recall measures labelled source quote recovery, not answer accuracy. Clarification rate also includes retrieval misses and conservative checks.',
            'Legal snapshots are not a certification of current law; source families do not infer cross-file legal hierarchy.',
            'No thresholds or prompts are tuned against validation/test in this run; individual failures retained.']
    finally:
        run_context.reset(token);await db.close()
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    rows=report['results']
    (folder/'results.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
    lines=['# 首轮 B1 RAG 基线','',f"- 配置：纯向量 top-5，无改写、BM25、Reranker；模型 `{report['knowledge_model']}`。",
        '- 候选标签尚无人工作为独立金标准复核；以下不是问答准确率。','', '```json',json.dumps(report['summary'],ensure_ascii=False,indent=2),'```','',
        '| 样例 | 划分 | 回答状态 | 原因 | 耗时 ms |','| --- | --- | --- | --- | --- |']
    lines += [f"| {r['id']} | {r['split']} | {r.get('result',{}).get('status',r['status'])} | {r.get('result',{}).get('reason_code',r.get('error_type',''))} | {r['duration_ms']} |" for r in rows]
    lines += ['', '逐例原文、定位、回答、引用和结构化支持审查见 report.json/results.jsonl。需要人工复核的案例全部保留；不可用引用存在率代替语义支持率。']
    (folder/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split',choices=['train','validation','test','all'],default='test')
    parser.add_argument('--output-dir',required=True);args=parser.parse_args();load_dotenv(ENV_PATH)
    folder=(PROJECT_ROOT/args.output_dir).resolve()
    if not folder.is_relative_to(PROJECT_ROOT/'.local/evaluation') or folder.exists():parser.error('Choose a NEW directory under .local/evaluation/')
    folder.mkdir(parents=True)
    try:
        report=asyncio.run(evaluate(args.split,folder))
        print(json.dumps({'passed':report['passed'],'summary':report['summary']},ensure_ascii=False,indent=2));return 0 if report['passed'] else 1
    except Exception as error:print(json.dumps({'passed':False,'error_type':type(error).__name__}));return 1


if __name__=='__main__':raise SystemExit(main())
