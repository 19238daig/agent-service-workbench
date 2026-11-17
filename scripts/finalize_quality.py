"""Assemble immutable quality evidence, review packet and preservation checks."""
import asyncio,csv,json,hashlib
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from statistics import mean
from dotenv import load_dotenv
from sqlalchemy import select,func,text
import httpx
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import KnowledgeEmbedding
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.evaluation.quality import aggregate
from smart_cs.evaluation.v2_assets import rows as dataset_rows,validate
from smart_cs.evaluation.v2_metrics import percentiles
from smart_cs.observability.context import usage_summary
from accept_runs_live import snapshot
from evaluate_quality import OUT,read,write,sha


def load(folder,pattern='*.json'):
    return [read(p) for p in sorted(folder.glob(pattern))]


def workflow_metrics(selected):
    happy=[r for r in selected if r['kind'] in {'create','read','transition'}]
    negative=[r for r in selected if r not in happy]
    calls=[c for r in selected for c in r.get('calls',[])]
    drafts=[r for r in selected if r.get('draft_created')]
    return {'selected':len(selected),'successful':sum(r.get('success',False) for r in selected),
        'task_success_including_errors':mean(bool(r.get('success')) for r in selected),
        'business_completion_selected':len(happy),'business_completed':sum(r.get('success',False) for r in happy),
        'business_completion_rate':mean(bool(r.get('success')) for r in happy) if happy else None,
        'negative_selected':len(negative),'correct_guard_outcomes':sum(r.get('success',False) for r in negative),
        'guard_outcome_rate':mean(bool(r.get('success')) for r in negative) if negative else None,
        'errors':[r['id'] for r in selected if r['status']!='evaluated'],
        'drafts_with_preapproval_probe':len(drafts),
        'preapproval_violations':sum(not r.get('guards',{}).get('before_consent_blocked',False) or not r.get('guards',{}).get('before_consent_unchanged',False) for r in drafts),
        'committed_workflows':sum(bool(r.get('after',{}).get('executions')) for r in selected),
        'failure_ids':[r['id'] for r in selected if not r.get('success')],
        'usage':usage_summary(calls),'physical_model_calls':len(calls),
        'known_reported_token_subtotal':sum(c['usage']['total_tokens'] or 0 for c in calls if c['kind']=='chat'),
        'latency_ms':percentiles([r['duration_ms'] for r in selected]),'estimated_usd':None}


def historical():
    b1=load(PROJECT_ROOT/'.local/evaluation/v2-b1-20261116-resume/models')
    # Join immutable labels before error denominators; raw prior files stay unchanged.
    labels={r.id:r for r in dataset_rows()}
    for r in b1:
        label=labels[r['id']]
        if r['task']=='intent':r.setdefault('expected_route',label.expected_route);r.setdefault('allowed_routes',label.allowed_routes)
        elif r['task']=='rag':
            r.setdefault('case_type',label.case_type);r.setdefault('answerable',label.answerable);r.setdefault('evaluation_scope',label.evaluation_scope)
        elif r['task']=='compliance':r.setdefault('expected',label.expected_decision)
    strict=aggregate(b1)
    write(OUT/'B1-strict-rescore.json',{'metrics':strict,'original_records_unchanged':True,'recomputed_with_phase26_formula':True,
         'historical_interrupted_attempt_usage_incomplete':True,'not_new_provider_run':True})
    hybrid=read(PROJECT_ROOT/'.local/hybrid/comparison.json');rerank=read(PROJECT_ROOT/'.local/reranker/comparison.json')
    performance=read(PROJECT_ROOT/'.local/performance/comparison.json')
    return {'B0':{'status':'not_comparably_measured','reason':'Legacy demonstrations have no frozen v2 full-quality baseline; missing metrics stay null.','metrics':None},
        'B1':{'status':'historical_components','metrics':strict,'source':'.local/evaluation/v2-b1-20261116-resume',
              'cautions':['Earlier interrupted/resumed experiment, different date/provider state and serving code.','Do not treat B1/B4 as a one-variable causal or noninferiority test.']},
        'B2':{'status':'retrieval_only','test':hybrid['by_split']['test'],'source':'.local/hybrid/comparison.json','generation_quality':None},
        'B3':{'status':'retrieval_only','test':rerank['test'],'source':'.local/reranker/comparison.json','generation_quality':None},
        'B4_retrieval':{'status':'paired_retrieval_equivalence','metrics':performance['metrics'],'canonical_equal':performance['all_canonical_evidence_equal'],
            'source':'.local/performance/comparison.json','whole_reply_speedup_proven':False}}


async def integrity():
    validate();before=read(OUT/'original-rows.json');after=await snapshot()
    assert before==after,'Original business/checkpoint rows changed during isolated evaluation'
    artifacts=read(OUT/'previous-artifacts.json')
    assert all(sha(PROJECT_ROOT/p)==value for p,value in artifacts.items()),'Prior configuration/report changed'
    db=configured_database()
    try:
        async with db.sessions() as s:
            index=await DenseRetriever(db).active(s)
            vectors=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index.id))
            revision=await s.scalar(text('SELECT version_num FROM alembic_version'))
            schemas=(await s.execute(text("SELECT nspname,pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname LIKE 'foundation_verify_%'"))).all()
            roles=(await s.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'foundation_verify_%'"))).scalars().all()
        assert vectors==547 and revision=='0015_knowledge_admin'
        assert not (OUT/'workflow-db-private.json').exists()
        # A root-owned historical verification schema predates this step. Never
        # remove another step's resources while cleaning this evaluator's roles.
        assert not roles,'Restricted temporary verification role remains'
        preserved=[]
        for name,owner in schemas:
            assert name=='foundation_verify_3cf4a886752d' and owner=='smart_cs'
            async with db.sessions() as s:
                earliest=await s.scalar(text('SELECT min(created_at) FROM foundation_verify_3cf4a886752d.users'))
            assert earliest.isoformat().startswith('2026-11-14')
            preserved.append({'schema':name,'fixture_created_at':earliest.isoformat(),'reason':'Root-owned historical schema predates phase26; retained untouched.'})
    finally:await db.close()
    async with httpx.AsyncClient(trust_env=False,timeout=15) as client:
        health=(await client.get('http://127.0.0.1:8000/health')).json()
    return {'passed':True,'unchanged_original_rows_and_checkpoints':sum(len(v) for v in before.values()),'tables':len(before),
            'all_original_rows_exactly_unchanged':True,'previous_artifacts_and_env_unchanged':True,
            'active_index':index.id,'vectors':vectors,'revision':revision,'temporary_resources_removed':True,'api_health':health,
            'historical_schema_preserved':preserved,
            'excluded_tables':['auth_sessions','auth_login_attempts']}


def review_packet(components,workflows):
    labels={r.id:r for r in dataset_rows()};records=[]
    for r in components:
        if r['task']!='rag':continue
        label=labels[r['id']]
        records.append({'id':r['id'],'kind':'rag','question':label.message,'answer':r.get('result',{}).get('answer'),
            'claims':r.get('result',{}).get('claims',[]),'citations':r.get('result',{}).get('citations',[]),
            'candidate_reference_points':label.reference_points,'control':label.control,'reason':r.get('reason_code'),
            'result_sha256':sha(OUT/'components'/(r['id']+'.json')),'review_status':'pending','human_correctness':None,
            'semantic_citation_support':None,'label_valid':None})
    for r in workflows:
        records.append({'id':r['id'],'arm':r['arm'],'kind':'workflow','turns':r['turns'],'final_state':r.get('final_state'),
            'receipt':r.get('receipt'),'before':r.get('before'),'after':r.get('after'),'guards':r.get('guards'),
            'computed_success':r.get('success'),'error_type':r.get('error_type'),'error_code':r.get('error_code'),
            'result_sha256':sha(OUT/'workflows/test'/(r['id']+'--'+r['arm']+'.json')),'review_status':'pending',
            'human_correctness':None,'label_valid':None,'approvals_are_synthetic':True})
    (OUT/'human-review.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
    (OUT/'human-review.md').write_text('# 第26步独立人工复核\n\n当前全部待复核；此清单不代表任何人工通过。\n先核对题目和候选标签，再逐句核对引用原文、范围、版本与结论；工单须核对本人身份、前置任务、审批阶段、实际回执和数据库变化。\n请在另存的复核记录中填写 reviewer、reviewed_at、label_valid、human_correctness、semantic_citation_support 和意见，不修改冻结原始结果；有争议的标签升级新版本。\n合成审批不是人工复核；规则通过和模型支持审查也不是独立语义正确性。\n\n'+''.join('- [ ] '+r['id']+(' / '+r['arm'] if r.get('arm') else '')+'\n' for r in records),encoding='utf-8')
    return {'cases':len(records),'rag':59,'paired_workflows':36,'independently_human_reviewed':0,'human_gold':False}


def readable(report,components,workflows):
    m=report['B4_components']['metrics'];intent=m['intent'];rag=m['rag']
    lines=['# 第26步质量与单/多Agent对照','','全部数值为冻结候选集上的测量，人工金标准尚未通过。',
        '','## B4完整组件测试','',
        f"共399项：意图300、RAG59、合规40。意图路由{intent['route_accuracy_including_errors']:.2%}，任务/实体联合{intent['joint_accuracy_including_errors']:.2%}；单标签宏F1={intent['macro_f1_including_errors']:.4f}（280项，20项多允许路由只从F1排除）。",
        f"RAG 32个可回答样本中发布{round(rag['answer_release_rate_including_errors']*32)}个；候选证据引用覆盖{rag['cited_reference_recall_including_errors']:.2%}，16个无答案样本的干净拒答率{rag['clean_refusal_including_errors']:.2%}。",
        f"{rag['literal_quote_anchors']}个逐字引用锚点；{rag['invalid_output']}个生成协议失败。4个注入控制没有金丝雀泄露，7个日期规则控制通过。锚点存在不等于结论语义正确；日期控制不等于7次真实问答。",
        '合规40项来自10个独立正文/状态组合，重复正文复用结果；模型/规则通过不能当作40个独立人工标签。',
        '', '## 成对真实业务工作流','',
        '12个合成案例×3组，7个正向办理/查询、5个拒绝/澄清/过期/拒批。固定模拟确认只发生在受限临时PostgreSQL中。',
        'A1=Gemini单控制器；M-shared=Gemini决策/检索/工单的多Agent；M-native=原DeepSeek/Gemini角色分配。三组共用GLM出站安全审查、资料、权限、工具与审批服务。',
        '主架构比较为A1与M-shared。M-native包含角色模型差异，不能把差异全部归因于编排。',
        '', '| 组 | 全部成功/12 | 正向完成/7 | 正确负向/5 | 错误 | 聊天调用 | p50/p95秒 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for arm,v in report['workflows'].items():
        lines.append(f"| {arm} | {v['successful']} | {v['business_completed']} | {v['correct_guard_outcomes']} | {len(v['errors'])} | {v['usage']['chat_calls']} | {v['latency_ms']['p50']/1000:.2f}/{v['latency_ms']['p95']/1000:.2f} |")
    lines+=['','本次没有证明多Agent完成率优势。A1两项错误中，一项是评测器采集空结果故障，修复后的补充验证不替换原始评分。',
        '多Agent主要失败原因包括TASK_ACTION_MISMATCH（工单草稿未与服务器任务参数完全一致）、出站审查阻止及缺槽；安全阻止避免写入，但属于未办完。',
        '成对逐例结果见 workflows/test/；失败保留在分母，安全拦截不自动等于任务完成。小样本、既有契约模板、非独立保留集，不能宣称单/多Agent普遍优劣或生产成功率。',
        '', '## B0–B4证据边界','',
        '| 版本 | 可报告内容 |', '| --- | --- |',
        '| B0 | 无可比v2正式全量记录，缺失值保留 |',
        '| B1 | 第22步399项组件，使用当前严格公式重算；保留中断、未知用量和原文件 |',
        '| B2 | 同48项检索；混合召回Recall@5由0.8125降至0.6875，未改默认 |',
        '| B3 | 混合重排排序Recall@5为0.8125，但阈值后覆盖0.625；部署dense阈值后0.6875 |',
        '| B4 | 同证据，检索p95冷885.38ms/暖321.09ms；本步另外测量399项完整组件 |',
        '', '当前严格口径下，历史B1可回答样本发布25/32，本次B4为23/32。不能从检索暖缓存加速推断整体质量改善；跨阶段差异也不能单独归因于B4缓存。',
        '', '不同阶段上下文预算、代码、时间及供应商别名状态存在差异，B1/B4不是单变量因果实验，也未建立端到端非劣效。',
        '', '## 用量、延迟与复核','',
        f"B4组件观察到{m['usage']['chat_calls']}次聊天调用、已报告Token合计{m['known_reported_token_subtotal']}；逐物理尝试列于 usage.csv。",
        '工作流耗时包含模型、检索、工具、固定模拟审批及数据库检查；验证/测试按顺序执行，并非生产负载测试。',
        '多Agent部分案例失败早停，较少调用或更短耗时不能解释为同样完成任务时更省或更快。',
        '网关没有提供别名价格，美元成本为null；一次验证预检输出序列化失败丢失详细用量，保留为未知，不能声称整个实验成本已完整统计。',
        '复核 human-review.md / human-review.jsonl；模型裁判、自检与规则结果不能填充人工正确率。',
        '', '## 复现','',
        '在项目根目录使用根目录.venv与.env：',
        '```powershell',r'.\.venv\Scripts\python.exe scripts/evaluate_quality.py --concurrency 2 --output-dir .local/quality-reproduction',
        r'.\.venv\Scripts\python.exe scripts/evaluate_agents.py --split test --output-dir .local/quality-workflow-reproduction',
        r'.\.venv\Scripts\python.exe scripts/evaluate_quality_contracts.py',
        r'.\.venv\Scripts\python.exe scripts/finalize_quality.py','```',
        '新输出目录会重新调用模型并冻结协议；默认目录仅在代码绑定相同时复用。脚本空结果修订后请用新目录，不覆盖本次记录或自动重放中断调用。外部模型为供应商别名，不能保证未来逐字输出相同。',
        '', '下一步27：交付级回归、迁移/备份恢复和新环境部署演练。']
    (OUT/'quality-report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    failures=['# 第26步失败与边界复核','', '以下为冻结输出，不以重跑挑选替代。','']
    for r in components:
        if r['task']=='rag' and (r.get('answer_status')!='answered' or r.get('reason_code')=='invalid_output'):
            failures.append('- '+r['id']+'：'+str(r.get('reason_code') or r.get('error_type') or 'date-control')+'；case_type='+str(r.get('case_type')))
    for r in workflows:
        if not r.get('success'):
            failures.append('- '+r['id']+' / '+r['arm']+'：'+str(r.get('error_code') or r.get('error_type') or '未满足冻结任务、发布或具体拒绝条件')+'；已观察工具='+str(r.get('observed_tools',[])))
    failures+=['','需人工逐条区分：召回缺失、阈值过滤、模型不支持/协议失败、漏提写任务、缺槽、合规拦截、模型服务故障、评测标签或严格评分误差。',
        '没有在本测试集上调整提示、阈值、角色模型或生产编排。',
        'foreign-order/A1的AttributeError属于评测器空结果采集故障，不能归因于模型；原错误保留在主结果分母。修复源代码及单独补充验证见workflow-harness-amendment.json与supplement/，不替换主评分。',
        '一次验证预检序列化失败保留started记录，用量未知；验证计数修订及初版报告均保留。']
    (OUT/'failure-review.md').write_text('\n'.join(failures)+'\n',encoding='utf-8')


async def main():
    components=load(OUT/'components');workflows=load(OUT/'workflows/test','*--*.json')
    assert len(components)==399 and len(workflows)==36
    assert all(r['status'] in {'evaluated','error'} for r in components+workflows)
    protocol=read(OUT/'workflows/test/protocol.json')
    amendment=read(OUT/'workflow-harness-amendment.json')
    changed={p for p,v in protocol['source'].items() if sha(PROJECT_ROOT/p)!=v}
    assert changed=={'scripts/evaluate_agents.py'}
    assert sha(PROJECT_ROOT/'scripts/evaluate_agents.py')==amendment['current_source_sha256']
    assert all(sha(OUT/'evaluated-source/workflow'/p)==v for p,v in protocol['source'].items())
    report={'created_at':datetime.now(timezone.utc).isoformat(),'completed':True,'human_gold':False,
            'B4_components':read(OUT/'component-summary.json'),'history':historical(),
            'workflows':{a:workflow_metrics([r for r in workflows if r['arm']==a]) for a in ['A1','M-shared','M-native']},
            'contracts':{k:v for k,v in read(OUT/'contract-summary.json').items() if k!='results'},
            'primary_architecture_comparison':['A1','M-shared'],'paired_outcomes':[],
            'general_multi_agent_superiority_proven':False,'independent_human_semantic_quality_proven':False,
            'all_experiment_billed_usage_known':False,'unknown_preflight_workflows':1,'workflow_harness_amendment':amendment}
    cases=[json.loads(line) for line in (PROJECT_ROOT/'data/agent-workflow-eval-v1/cases.jsonl').read_text(encoding='utf-8').splitlines()]
    compound={c['id'] for c in cases if c['split']=='test' and len(c['required'])>1}
    report['compound_workflows']={a:workflow_metrics([r for r in workflows if r['arm']==a and r['id'] in compound]) for a in report['workflows']}
    report['failure_task_codes']=dict(Counter(t.get('reason_code') for r in workflows for t in r.get('final_state',{}).get('task_plan',{}).get('tasks',[]) if t.get('reason_code')))
    report['supplement']=read(OUT/'supplement/check.json')
    assert report['supplement']['passed']
    for identifier in sorted({r['id'] for r in workflows}):
        report['paired_outcomes'].append({'id':identifier,**{a:next(r for r in workflows if r['id']==identifier and r['arm']==a)['success'] for a in report['workflows']}})
    report['review']=review_packet(components,workflows);report['integrity']=await integrity()
    with (OUT/'usage.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        fields=['phase','case','arm','attempt','kind','model','status','error_code','duration_ms','usage_status','input_tokens','output_tokens','total_tokens']
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        validation=[read(p) for p in sorted((OUT/'workflows/validation').glob('*--*.json')) if '.initial.' not in p.name]
        supplement=load(OUT/'supplement','*--*.json')
        for phase,selected in [('B4_components',components),('workflow_validation',validation),('workflow_test',workflows),('supplement_not_primary',supplement)]:
            for r in selected:
                for n,c in enumerate(r.get('calls',[]),1):
                    writer.writerow({'phase':phase,'case':r['id'],'arm':r.get('arm'),'attempt':n,**{k:c.get(k) for k in ['kind','model','status','error_code','duration_ms']},
                        'usage_status':c['usage']['status'],**{k:c['usage'].get(k) for k in ['input_tokens','output_tokens','total_tokens']}})
    write(OUT/'comparison.json',report);readable(report,components,workflows)
    artifacts={p.relative_to(OUT).as_posix():sha(p) for p in OUT.rglob('*') if p.is_file() and p.name not in {'final-acceptance.json','evidence-manifest.json'}}
    write(OUT/'evidence-manifest.json',artifacts)
    acceptance={'completed':True,'step':26,'component_cases':399,'paired_workflow_cases':36,'human_gold':False,
        'contract_counts':report['contracts']['counts'],'integrity':report['integrity'],
        'single_vs_multi_general_superiority_proven':False,'all_billed_usage_known':False,'next_step':27,
        'comparison_sha256':sha(OUT/'comparison.json'),'manifest_sha256':sha(OUT/'evidence-manifest.json')}
    write(OUT/'final-acceptance.json',acceptance);print(json.dumps({k:v for k,v in acceptance.items() if k!='integrity'},ensure_ascii=False))


if __name__=='__main__':load_dotenv(ENV_PATH);asyncio.run(main())
