"""B4 technical acceptance. Evidence equivalence is not end-to-end noninferiority."""
import asyncio,json,subprocess
from datetime import datetime,timezone
from copy import deepcopy
from dotenv import load_dotenv
import httpx
from sqlalchemy import select,func,text
from authenticated_client import authenticated_client
from accept_runs_live import snapshot
from evaluate_performance import OUT,bind,read,write,sha,PROFILE
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import KnowledgeEmbedding
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.performance.policy import ServingPolicy,fingerprint
from smart_cs.evaluation.v2_assets import validate,review_status


async def integrity(selection):
    db=configured_database()
    try:
        now=await bind(db);old=deepcopy(selection['binding']);amend=read(OUT/'dashboard-amendment.json')
        key=amend['source'];assert now['code_sha256'][key]==amend['current_sha256']
        assert old['code_sha256'][key]==amend['evaluated_sha256']
        old['code_sha256'][key]=now['code_sha256'][key];assert now==old
        async with db.sessions() as s:
            index=await DenseRetriever(db).active(s)
            count=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index.id))
            revision=await s.scalar(text('SELECT version_num FROM alembic_version'))
        assert count==547 and revision=='0015_knowledge_admin'
        return {'index':index.id,'vectors':count,'revision':revision,'serving_sources_frozen':True,'dashboard_only_additive_amendment':True}
    finally:await db.close()


async def preservation():
    before=read(OUT/'original-rows.json');after=await snapshot();assert set(before)==set(after)
    allowed={'chat_sessions','chat_turns','chat_messages','execution_runs','run_controls','run_events','model_call_records',
             'checkpoint:checkpoints','checkpoint:checkpoint_blobs','checkpoint:checkpoint_writes'}
    additions={};unchanged=0
    for table,records in before.items():
        assert all(after[table].get(k)==v for k,v in records.items()),'Old row changed: '+table
        unchanged+=len(records);n=len(set(after[table])-set(records))
        if n:assert table in allowed;additions[table]=n
    assert additions.get('chat_sessions')==4 and additions.get('chat_turns')==6 and additions.get('chat_messages')==12
    return {'passed':True,'tables':len(before),'unchanged_old_rows_and_checkpoints':unchanged,
            'only_added_readonly_chats':additions,'orders_tickets_approvals_knowledge_unchanged':True,'auth_sessions_and_login_attempts_excluded':True}


def backend():
    raw=read(OUT/'backend-verification.json');rerun=read(OUT/'regression-reruns.json');assert rerun['passed']
    modules=deepcopy(raw['modules'])
    assert {n for n,r in modules.items() if not r['passed']}=={'test_grounded_answers'}
    for name,result in rerun['modules'].items():assert result['passed'];modules[name]=result
    result={'passed':all(r['passed'] for r in modules.values()),'tests_run':sum(r['tests_run'] for r in modules.values()),
            'skipped':sum(r['skipped'] for r in modules.values()),'modules':modules,
            'original_failure_preserved':True,'original_report_sha256':sha(OUT/'backend-verification.json'),
            'rerun_sha256':sha(OUT/'regression-reruns.json'),'scope':'Affected paths, not a new full repository suite'}
    assert result['passed'];write(OUT/'backend-final.json',result);return result


def workers():
    code="import json,os;from smart_cs.performance.policy import ServingPolicy,fingerprint;print(json.dumps({'profile':ServingPolicy.deployed().model_dump(),'fingerprint':fingerprint(),'container_cache_disabled':os.getenv('SMART_CS_WORKER_CONTAINER')=='1'}))"
    process=subprocess.run(['docker','compose','-p','smart-cs-foundation','--profile','jobs','exec','-T','worker','python','-c',code],cwd=PROJECT_ROOT,capture_output=True,text=True,timeout=40)
    assert process.returncode==0;value=json.loads(process.stdout)
    assert value['profile']==ServingPolicy.deployed().model_dump() and value['fingerprint']==fingerprint() and value['container_cache_disabled']
    return {'passed':True,'profile_sha256':fingerprint(),'worker_uses_same_budgets':True,'cache_disabled_in_worker':True}


def readable(comparison,checks):
    lines=['# 第25步 B4报告','','技术验收通过；没有证明端到端质量非劣效或所有请求更快。','','## 冻结证据实验','','| 条件 / 48查询 | Recall | 本地推理 | 缓存命中 | p50(ms) | p95(ms) |','| --- | ---: | ---: | ---: | ---: | ---: |']
    for mode,v in comparison['metrics'].items():lines.append(f"| {mode} | {v['context_recall']:.4f} | {v['local_model_attempts']} | {v['cache_hits']} | {v['latency_ms']['p50']:.2f} | {v['latency_ms']['p95']:.2f} |")
    lines+=['','所有候选、原文、元数据及最终证据一致；冷缓存有额外开销，暖缓存不是生产命中率。','','## 真实只读运行','','| 阶段 | 场景 | 首个持久化进度(ms) | 最终客户端时间(ms) | 聊天调用 | 本地推理 | 结果 |','| --- | --- | ---: | ---: | ---: | ---: | --- |']
    for phase in ['B3','B4']:
        for i in range(3):
            r=read(OUT/f'live-{phase}-{i}.json');status=r['knowledge_status'].get('status') or '意图澄清，未进入知识分支'
            lines.append(f"| {phase} | {['首次','新会话重复','带历史后续'][i]} | {r['first_persisted_progress_ms']:.1f} | {r['final_client_ms']:.1f} | {sum(c['kind']=='chat' for c in r['calls'])} | {sum(c['kind']!='chat' for c in r['calls'])} | {status} |")
    lines+=['','本次整轮时间未显示加速；不能用B4后续问题未完成知识任务的较短耗时宣传性能收益。','',
            '直接知识Agent的相同后续问题对照：3→2次聊天调用，两次均带引用回答；其范围不包含意图与出站合规。','',
            f"受影响后端检查{checks['tests_run']}项，其中跳过{checks['skipped']}项；另有20项真实PostgreSQL/Redis专项及32项浏览器交互回归。",'',
            '52条角色模型试验不足自动更换角色模型。原始失败、路由澄清、测试前修订、逐次用量均保留。','',
            '详见 ../../docs/phase-twenty-five-performance.md；下一步26进行完整质量与单Agent/多Agent对照。']
    (OUT/'B4-report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (OUT/'failure-review.md').write_text('# 第25步失败及限制复核\n\n- 冷缓存增加检索开销；生产命中率未知。\n- B4后续问题被意图模型以0.62置信度澄清，不能算任务成功或改写节省。见 live-B4-2.json。\n- 真实整轮重复问题的时间没有缩短，保留全部6次测量。\n- 模型试验存在候选契约未命中，52条不是52条正确。见 model-pilot/。\n- 原后端超时类型兼容失败已修复；原日志及定向重跑均保存。\n- 人工金标准仍未通过，端到端非劣效未证明。\n\n[ ] 复核模型输入/候选标签；[ ] 复核路由澄清；[ ] 在第26步建立完整质量与成本对照；[ ] 对新业务和新资料另设保留集。\n',encoding='utf-8')


def main():
    load_dotenv(ENV_PATH);selection=read(OUT/'selection.json');comparison=read(OUT/'comparison.json')
    assert comparison['completed'] and comparison['all_canonical_evidence_equal'] and comparison['B3_matches_step24']
    assert comparison['selection_sha256']==sha(OUT/'selection.json') and sha(PROFILE)==selection['profile_sha256']
    assert selection['test_labels_used'] is False and comparison['test_used_for_selection'] is False
    for name,h in read(OUT/'previous-artifacts.json').items():assert sha(PROJECT_ROOT/name)==h,'Preserved asset changed: '+name
    serving=asyncio.run(integrity(selection));data=asyncio.run(preservation());checks=backend()
    pg=read(OUT/'postgresql-verification.json');assert pg['passed'] and pg['tests']==20 and pg['public_tables_unchanged']
    ui=read(OUT/'browser-regression.json')['stats'];assert ui['expected']==32 and ui['unexpected']==0
    browser=read(OUT/'browser-live.json');assert browser['passed'] and len(browser['sizes'])==5
    original=read(OUT/'live-B3-summary.json');live=read(OUT/'live-B4-summary.json');direct=read(OUT/'rewrite-live.json')
    assert original['passed'] and live['passed'] and direct['passed']
    assert live['runs'][1]['performance']['cache_hits']==2 and live['followup_route_did_not_exercise_knowledge']
    with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=15) as client:
        assert client.get('/api/admin/performance').status_code==401
        with authenticated_client(client,'customer-a'):assert client.get('/api/admin/performance').status_code==403
        with authenticated_client(client,'admin'):
            response=client.get('/api/admin/performance');response.raise_for_status();dashboard=response.json()
            assert all('known_input_tokens' in r and 'known_output_tokens' in r for r in dashboard['models']);write(OUT/'live-dashboard.json',dashboard)
        assert client.get('/health/ready').status_code==200
    worker=workers();readable(comparison,checks)
    report={'passed':True,'technical_checks_passed':True,'step':25,'checked_at':datetime.now(timezone.utc).isoformat(),
            'retrieval_quality_equivalent':True,'end_to_end_noninferiority_proven':False,'overall_reply_speedup_proven':False,
            'candidate_labels_pending_independent_review':review_status(PROJECT_ROOT/'.local/eval-v2/reviews.jsonl'),
            'serving':serving,'database':data,'backend':checks,'postgresql_redis':pg,'browser_regression_tests':32,
            'browser_live':browser,'worker':worker,'real_provider_readonly_runs':6,'direct_agent_comparisons':2,
            'role_model_records':52,'models_unchanged':True,'root_env_and_previous_reports_unchanged':True,
            'comparison_sha256':sha(OUT/'comparison.json'),'selection_sha256':sha(OUT/'selection.json'),'dataset':validate(),
            'limitations':['Cold cache overhead.','One B4 followup took clarification, not task success.',
                           'No production hit rate, throughput, end-to-end accuracy or hard monetary budget claim.'],
            'next_step':26}
    write(OUT/'final-acceptance.json',report)
    print(json.dumps({'passed':True,'backend_tests':checks['tests_run'],'skipped':checks['skipped'],'unchanged_old_rows':data['unchanged_old_rows_and_checkpoints'],
                      'end_to_end_quality_proven':False,'overall_speedup_proven':False}))


if __name__=='__main__':main()
