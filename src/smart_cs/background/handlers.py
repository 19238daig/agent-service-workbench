"""Fixed job registry; broker messages cannot select functions or file paths."""
import asyncio
import hashlib
import json
from statistics import mean
from smart_cs.schemas.jobs import JobRequest
from smart_cs.schemas.errors import ApplicationError
from smart_cs.background.files import JobFiles
from smart_cs.settings import PROJECT_ROOT

async def perform(context,fenced):
    work=JobRequest.model_validate({'idempotency_key':context.job.idempotency_key,'work':context.job.payload}).work
    if work.kind=='ingest':
        from smart_cs.ingestion.pipeline import IngestionPipeline
        path=await asyncio.to_thread(JobFiles().verify,work.artifact_sha256,work.extension)
        await context.progress('parsing',0,1)
        spec=work.document.model_copy(update={'expected_sha256':work.artifact_sha256})
        result=await IngestionPipeline(fenced).ingest(path,spec,work.options,publish=work.publish)
        await context.receipt(result)
        await context.progress('document_committed',1,1)
        return result
    if work.kind=='index':
        from smart_cs.retrieval.indexes import DenseIndexBuilder
        await context.progress('embedding_and_indexing',0,1)
        result=await DenseIndexBuilder(fenced).build(activate=False,progress=context.progress)
        await context.receipt(result)
        await context.progress('index_staged',result['chunks'],result['chunks'])
        return result
    return await evaluate(context,work.split)

async def evaluate(context,split):
    from smart_cs.evaluation.assets import ROOT,CORPUS,validate,records
    from smart_cs.evaluation.schema import RagCase
    from smart_cs.evaluation.runner import evidence_scores
    from smart_cs.retrieval.dense import DenseRetriever
    # Full source/label validation runs off the event loop; frozen test is excluded.
    await asyncio.to_thread(validate,ROOT,CORPUS)
    manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
    retriever=DenseRetriever(context.db)
    async def bound():
        async with context.db.sessions() as s:index=await retriever.active(s)
        if context.job.binding!={'index_id':index.id,'dataset_sha256':hashlib.sha256((ROOT/'manifest.json').read_bytes()).hexdigest()} or index.configuration.get('source_manifest_hash')!=manifest['source_manifest_sha256']:
            raise ApplicationError('JOB_EVALUATION_BINDING_CHANGED','索引或冻结评测资产发生变化，请提交新任务。',409)
    await bound()
    cases=[r for r in records(ROOT) if isinstance(r,RagCase) and r.split==split and r.evaluation_scope=='retrieval']
    completed=dict(context.job.checkpoint.get('cases',{}))
    for case in cases:
        await bound()
        if case.id in completed:continue
        await context.progress('retrieval_evaluation',len(completed),len(cases),{'cases':completed})
        result=await retriever.search(case.message,20)
        if result['index_id']!=context.job.binding['index_id']:raise ApplicationError('JOB_EVALUATION_BINDING_CHANGED','活动索引已经改变。',409)
        row={'id':case.id,'answerable':case.answerable,'retrieved':[d['id'] for d in result['documents'][:5]]}
        if case.answerable:row.update(at_5=evidence_scores(case,result['documents'],5),at_20=evidence_scores(case,result['documents'],20))
        completed[case.id]=row
        await context.progress('retrieval_evaluation',len(completed),len(cases),{'cases':completed})
    await bound()
    scored=[r for r in completed.values() if 'at_5' in r]
    return {'kind':'exact_dense_development_diagnostic','binding':context.job.binding,'split':split,'results':list(completed.values()),
        'human_reviewed':False,'gold_standard':False,'chat_model_calls':0,'reranker_calls':0,'questions_or_labels_indexed':False,
        'summary':{'cases':len(completed),'answerable':len(scored),'mean_evidence_recall_at_5':mean(r['at_5']['evidence_recall'] for r in scored) if scored else None},
        'pass_meaning':'Diagnostic completed; not a gold-label quality acceptance claim'}
