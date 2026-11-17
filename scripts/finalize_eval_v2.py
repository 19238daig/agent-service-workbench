"""Candidate delivery gates, B1 failure accounting, and read-only database audit."""
import asyncio
from collections import Counter
from datetime import datetime,timezone
import json
from pathlib import Path
import httpx
from dotenv import load_dotenv
from sqlalchemy import select,func
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import DatasetRelease,DatasetSample,RetrievalIndex,KnowledgeEmbedding
from smart_cs.evaluation.v2_assets import ROOT,RELEASE,rows,validate,review_status,sha,encoded
from smart_cs.evaluation.v2_runner import read,write,check_config
from accept_runs_live import snapshot

OUT=PROJECT_ROOT/'.local/eval-v2'
RUN=PROJECT_ROOT/'.local/evaluation/v2-b1-20261116-resume'


async def preservation():
    before=read(OUT/'original-rows.json');after=await snapshot()
    for name,values in before.items():
        assert all(after[name].get(k)==v for k,v in values.items()),'Original rows changed: '+name
        if name not in {'dataset_releases','dataset_samples'}:assert values==after[name],'Unexpected evaluation database effects: '+name
    assert len(after['dataset_releases'])-len(before['dataset_releases'])==1
    assert len(after['dataset_samples'])-len(before['dataset_samples'])==2100
    db=configured_database()
    try:
        await check_config(RUN,db)
        async with db.sessions() as s:
            registered=await s.get(DatasetRelease,RELEASE);assert registered.manifest_hash==validate()['manifest_sha256']
            samples=list((await s.scalars(select(DatasetSample).where(DatasetSample.release_id==RELEASE))).all())
            expected={RELEASE+':'+r.id:r.model_dump() for r in rows()};assert len(samples)==2100
            assert all(expected.get(r.id)==r.payload for r in samples)
            index=await s.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
            vectors=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index.id))
        return {'original_rows_preserved':True,'original_checkpoints_preserved':True,'knowledge_and_business_tables_unchanged':True,
            'new_dataset_releases':1,'new_dataset_samples':2100,'registered_payloads_verified':True,'active_index':index.id,'active_vectors':vectors}
    finally:await db.close()


def human_report(report,retrieval,models,contracts):
    dataset=report['dataset'];intent=models['metrics']['intent'];rag=models['metrics']['rag'];comp=models['metrics']['compliance']
    lines=['# 第22步 B1 候选基线报告','',
        '本次冻结的是待独立人工复核的候选数据。人工金标准为0；以下指标不是生产准确率或独立盲测成绩。',
        '用户已选择后续自行复核，本次不生成任何实际批准记录。','',
        '| 数据 | 总数 | train | validation | test |','| --- | ---: | ---: | ---: | ---: |']
    for task in ['intent','rag','ticket_workflow','compliance']:
        c=dataset['counts_by_split'];lines.append(f"| {task} | {dataset['counts'][task]} | {c.get(task+'/train',0)} | {c.get(task+'/validation',0)} | {c.get(task+'/test',0)} |")
    lines += ['', '意图150个场景族形成1500条表达；RAG175个场景/控制族形成300条；合规25组允许/拒绝配对形成200条；工单100项各回链一条可执行契约测试。模板改写和相同正文复用不是独立业务样本。',
        '', '## 固定测量条件','',
        '- 既有真实 BGE-M3 及 pgvector 精确余弦 top20，回答上下文 top5；无查询改写、BM25、Reranker。线上工作台配置保持不变。',
        '- 检索覆盖全部300条RAG：250条进入检索，其中200条有证据；50条注入/假设日期控制不计入检索质量。',
        '- 模型测量只运行冻结test：300意图、59 RAG、40合规；相同合规正文/状态只实际判断一次，明确标记复用。日期控制只检查确定性日期规则。',
        '- 工单100项在隔离测试库执行，不是100条真实模型业务对话；环境不满足的用例明确skip。',
        '- 原实验于2026-11-15中断；276条完整结果按原始文件哈希继承，4条中断明确重试，119条补跑。离线检索/回答时钟固定为原业务日期2026-11-15，不跟随恢复日2026-11-16变化。原目录完整保留，恢复代码差异见recovery-code.diff。',
        '- 原4条中断尝试的供应商调用次数与Token用量未知；下述usage仅汇总已完成并保存的调用，不能当成包含中断成本的总账。供应商相同模型别名背后的修订版本无法独立确认。',
        '- model名、温度、超时、endpoint哈希、依赖版本、代码/数据/索引哈希及日期均绑定run.json；没有把参考答案或标签送入模型。',
        '', '## 测量结果','',
        f"- 200条有证据题：候选证据Recall@5 = {retrieval['candidate_evidence_recall_at_5']:.4f}，Recall@20 = {retrieval['candidate_evidence_recall_at_20']:.4f}，MRR@5 = {retrieval['candidate_mrr_at_5']:.4f}。",
        f"- 按{retrieval['evidence_groups']}个连接证据组等权汇总Recall@5 = {retrieval['mean_group_evidence_recall_at_5']:.4f}；普通题目均值会受改写题重复权重影响。",
        f"- 冻结test有证据题数 {retrieval['by_split']['test']['scored']}，其Recall@5 = {retrieval['by_split']['test']['recall_at_5']:.4f}。开发与测试分别报告，未据此调阈值。",
        f"- 意图路由候选标签匹配率（错误计入分母）：{intent['route_accuracy_including_errors']:.4f}；路由、任务、实体联合匹配率：{intent['joint_route_tasks_entities_including_errors']:.4f}。",
        f"- 单一路由、格式有效子集macro-F1：{intent['macro_f1_evaluated_single_route']}（分母 {intent['single_route_f1_denominator']}）；不含允许多个路由的复合任务，不与总体路由匹配率混作同一口径。",
        f"- RAG test结果：{json.dumps(rag,ensure_ascii=False)}",
        f"- 合规 test结果：{json.dumps(comp,ensure_ascii=False)}",
        f"- 工单契约：{json.dumps(contracts['counts'],ensure_ascii=False)}。",
        f"- 模型调用/Token统计：{json.dumps(models['usage'],ensure_ascii=False)}。usage缺失不按0计，未按虚构单价换算费用。",'',
        '## 保留的失败与范围','',
        f"- 检索top5未覆盖全部标注证据：{len(retrieval['failed_evidence_cases'])}条，完整ID见retrieval-summary.json；不能删题后重新报分。",
        f"- 模型技术/格式异常：{len(models['metrics']['errors'])}条，保留逐例原始可审查结果；无效模型格式不会当作正确unknown，服务不可用导致的fail-closed不会当作合规成功。",
        '- 模型拒答、引用锚定失败、错误路由与漏任务均保存到failure-review.md。回看冻结测试只作诊断，不在本步调参重测提分。',
        '- 同一模型的草稿与支持审查不是独立裁判；字面引用命中不等于语义正确。人工答案正确率、独立引用支持率与生产注入安全性均未给出数值。',
        '- Ticket Actions保留文档未进入旧RAG标签或本版开发标签，但早已存在服务知识库。仅2条表达/1个场景，不能据此声称新文档泛化能力；预训练暴露未知。',
        '- 25条version题是假设时间元数据控制，不是25个真实法律冲突裁决；新文档独立盲测和人工金标准仍需补充。',
        '- 所有原有业务、知识和检查点行保持不变；数据库仅新增1条release和2100条样本，现用索引保持不变。',
        '', '## 人工复核入口','',
        '- data/agent-eval-v2/ANNOTATION.md：复核规范；review/intent.md、rag.md、ticket_workflow.md、compliance.md：逐条候选。',
        '- scripts/review_eval_v2.py status：查看未复核数量；record --help：记录实际人工决定。决定绑定case与manifest哈希，保存在.local/eval-v2/reviews.jsonl。',
        '- 标签改动发布新版本，不原地修改本版。后续B2开发只使用train/validation，冻结test作为里程碑。',
        '', f"最终验收记录：.local/eval-v2/final-acceptance.json；完整B1目录：{RUN.relative_to(PROJECT_ROOT).as_posix()}。"]
    (OUT/'B1-report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def failure_review(model_rows,retrieval):
    index={r.id:r for r in rows()};lines=['# B1候选逐例失败与待人工检查','',
        '标签未通过独立人工复核。以下包含系统失败、候选标签争议和保守拒答，不直接判为真实业务错误。不得在本次冻结test上调参提分。','']
    failed=[]
    for r in model_rows:
        reasons=[]
        if r.get('status')!='evaluated':reasons.append(r.get('error_type',r['status']))
        elif r['task']=='intent' and not r.get('joint_correct'):reasons.append('路由/任务/实体未联合匹配候选标签')
        elif r['task']=='compliance' and not r.get('correct'):reasons.append('内容决定未匹配候选标签')
        elif r['task']=='rag' and r.get('case_type') in {'direct','multi_evidence'} and (r.get('answer_status')!='answered' or not r.get('cited_reference',{}).get('all_groups')):reasons.append('答复未发布或引用未覆盖全部标注证据')
        elif r.get('model_format_error'):reasons.append('模型输出格式或锚定未通过')
        elif r['task']=='rag' and r.get('case_type')=='unanswerable' and r.get('answer_status')=='answered':reasons.append('不可答候选却发布了回答，需复核标签与答复')
        if reasons:
            failed.append(r['id']);case=index[r['id']]
            lines += [f"## {r['id']}",f'- 问题：{case.message}',f'- 原因：{"；".join(reasons)}',f'- 逐例结果：../evaluation/v2-b1-20261116-resume/models/{r["id"]}.json',f'- family/group：{r.get("family")} / {r["group_id"]}','- [ ] 复核标签；[ ] 复核来源；[ ] 复核模型输出；[ ] 分类根因','']
    lines += ['## 检索证据缺失ID','',*['- '+x for x in retrieval['failed_evidence_cases']]]
    (OUT/'failure-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');return failed


def main():
    load_dotenv(ENV_PATH);OUT.mkdir(exist_ok=True);report={'passed':False,'step':22,'checked_at':datetime.now(timezone.utc).isoformat(),'delivery_scope':'frozen candidates plus B1; human review by user later'}
    try:
        dataset=validate();review=review_status(OUT/'reviews.jsonl');retrieval=read(RUN/'retrieval-summary.json');models=read(RUN/'model-summary.json');contracts=read(RUN/'contract-summary.json');tests=read(PROJECT_ROOT/'.local/checks/modules/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/test_evaluation_v2.json');resume_tests=read(PROJECT_ROOT/'.local/checks/modules/cccccccccccccccccccccccccccccccc/test_evaluation_resume.json')
        assert tests['passed'] and tests['tests_run']==13 and resume_tests['passed'] and resume_tests['tests_run']==2
        assert retrieval['completed'] and not retrieval['errors'] and models['completed'] and models['selected']==399
        assert contracts['completed'] and contracts['selected']==100 and not any(contracts['counts'].get(k,0) for k in ['failed','error','not_run'])
        model_rows=[read(p) for p in sorted((RUN/'models').glob('*.json'))];assert len(model_rows)==399 and not any(r['status']=='started' for r in model_rows)
        expected={r.id for r in rows() if r.split=='test' and r.task!='ticket_workflow'};assert {r['id'] for r in model_rows}==expected
        assert any(r['status']=='evaluated' for r in model_rows)
        recovery=read(RUN/'recovery.json');assert recovery['adopted_completed_results']==276
        for item in recovery['adopted']:
            original=PROJECT_ROOT/item['source'];assert sha(original.read_bytes())==item['source_sha256']
        db=asyncio.run(preservation())
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=10) as c:assert c.get('/').status_code==c.get('/health').status_code==200
        failures=failure_review(model_rows,retrieval)
        report.update(passed=True,technical_checks_passed=True,dataset=dataset,independent_human_review=review,formal_gold_gate_passed=review['gold_gate_passed'],
            baseline_complete=True,baseline_technical_or_format_errors=models['metrics']['errors'],candidate_failure_review_cases=len(failures),
            new_integrity_tests=15,contracts=contracts['counts'],database=db,live_service_healthy=True,
            run_directory=RUN.relative_to(PROJECT_ROOT).as_posix(),recovery={k:v for k,v in recovery.items() if k!='adopted'},next_step=23,
            limitations=['Not independently human-reviewed gold.','Template variants are dependent.','No production accuracy or safety claim.','Within-corpus document holdout has only 2 cases.'])
        human_report(report,retrieval,models,contracts)
        write(OUT/'gates.json',{'candidate_data_frozen':True,'B1_milestone_complete':True,'independent_human_review_complete':review['gold_gate_passed'],'formal_gold_release':False,'threshold_or_prompt_tuned_on_test':False,'user_will_review_later':True})
    except Exception as error:report.update(error_type=type(error).__name__,error=str(error) if isinstance(error,AssertionError) else 'See individual evaluation artifacts; no secrets recorded')
    write(OUT/'final-acceptance.json',report);print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
