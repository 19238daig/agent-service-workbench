"""Step 24: provenance, real-model integration, regression and old-row preservation."""
import asyncio
from datetime import datetime, timezone
import json
import subprocess

from dotenv import load_dotenv
from sqlalchemy import func, select, text
from accept_runs_live import snapshot
from evaluate_reranker import OUT, B2, bind, read, write, sha, legacy
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import KnowledgeEmbedding, ChatSession
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.retrieval.reranking import PROFILE, RerankConfig, digest
from smart_cs.evaluation.v2_assets import validate, review_status


async def preserve(live, focused):
    before = read(OUT/'original-rows.json'); after = await snapshot(); assert set(before) == set(after)
    added = {}; unchanged = 0
    allowed = {'chat_sessions', 'chat_turns', 'chat_messages', 'execution_runs', 'run_controls',
               'run_events', 'model_call_records', 'checkpoint:checkpoints', 'checkpoint:checkpoint_blobs', 'checkpoint:checkpoint_writes'}
    for table, records in before.items():
        assert all(after[table].get(key) == value for key, value in records.items()), 'Existing row changed: '+table
        unchanged += len(records); count = len(set(after[table])-set(records))
        if count:
            assert table in allowed, 'Unexpected new business rows: '+table
            added[table] = count
    assert added.get('chat_sessions') == 2 and added.get('chat_messages') == 4
    db = configured_database()
    try:
        async with db.sessions() as s:
            index = await DenseRetriever(db).active(s)
            vectors = await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id == index.id))
            revision = await s.scalar(text('SELECT version_num FROM alembic_version'))
            for report in [live, focused]:
                session = await s.get(ChatSession, report['chat']['session_id'])
                assert session is not None and session.active_run_id is None
        assert index.id == read(B2/'comparison.json')['binding']['index_id'] and vectors == 547
        assert revision == '0015_knowledge_admin'
        return {'passed': True, 'unchanged_old_rows_and_checkpoint_entries': unchanged, 'tables': len(before),
                'added_only_new_readonly_chat_records': added, 'active_index': index.id, 'vectors': vectors,
                'migration_revision': revision, 'orders_tickets_approvals_and_knowledge_unchanged': True,
                'auth_sessions_and_login_attempts_excluded': True}
    finally:
        await db.close()


def worker():
    probe = (
        "import asyncio,json,httpx;from smart_cs.retrieval.reranking import CrossEncoderReranker,RerankConfig,digest;"
        "p=RerankConfig.deployed();"
        "print(json.dumps({'profile':p.model_dump(),'profile_sha256':digest(p.model_dump())}));"
        "r=httpx.get(CrossEncoderReranker(p).url+'/identity',trust_env=False,timeout=15);r.raise_for_status();"
        "from smart_cs.retrieval.reranking import validate_identity;"
        "i=validate_identity(r.json(),p.expected_identity);print(json.dumps({'server_identity_sha256':i['sha256']}))"
    )
    result = subprocess.run(['docker', 'compose', '-p', 'smart-cs-foundation', '--profile', 'jobs',
                             'exec', '-T', 'worker', 'python', '-c', probe], cwd=PROJECT_ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, 'Updated worker profile/identity check failed'
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert rows[0]['profile'] == RerankConfig.deployed().model_dump()
    assert rows[1]['server_identity_sha256'] == RerankConfig.deployed().expected_identity
    return {'passed': True, 'profile_sha256': rows[0]['profile_sha256'], 'model_identity': rows[1]['server_identity_sha256'],
            'profile_matches_windows_api': True, 'local_model_service_reachable': True}


def reports(comparison, selection, backend, live, focused):
    lines = ['# 第24步：B2/B3 与纯向量控制', '',
             '候选标签待独立人工复核；只测检索和证据，不代表问答准确率。参数与部署决策在冻结测试前确定。', '',
             '| 冻结测试 / 32正例 | 排名Recall@5（未过滤） | 最终证据Recall | 最终证据MRR | 实际证据数均值 | 正文字符均值 |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for label in ['B2', 'B3', 'legacy_dense', 'online_dense']:
        r = comparison['test'][label]
        lines.append(f"| {label} | {r['ranking_recall_at_5']:.4f} | {r['context_recall']:.4f} | {r['context_mrr']:.4f} | {r['mean_context_count']:.4f} | {r['mean_context_characters']:.2f} |")
    lines += ['', 'B2/B3最终证据上限均为3。B3排序Recall增加，但0.10阈值过滤后的证据覆盖下降，不能声称混合B3全面优于B2。', '',
              '纯向量新配置保持同一模型，证据上限3→5，截断512→256；排名Recall@5相同，最终证据覆盖增加伴随更多上下文。', '',
              '| 完整流水线延迟 / 48查询 | p50(ms) | p95(ms) |', '| --- | ---: | ---: |']
    for label, latency in comparison['test_latency_ms'].items():
        lines.append(f"| {label} | {latency['p50']:.2f} | {latency['p95']:.2f} |")
    delta = comparison['B3_paired_added_latency_ms']
    lines += ['', f"B3配对新增耗时：p50={delta['p50']:.2f}ms，p95={delta['p95']:.2f}ms。", '',
              f"测试中B3证据覆盖改善{len(comparison['test_improved'])}条、退步{len(comparison['test_regressed'])}条。截断{comparison['truncated_pairs_test_B3']}对输入。", '',
              '部署继续dense；重排配置：`'+json.dumps(selection['deployment_profile'])+'`。', '',
              f"回归{backend['tests_run']}项，其中跳过{backend['skipped']}项；另有21项真实PostgreSQL专项。", '',
              f"真实广泛政策问题：{live['chat']['status']}；明确政策问题：{focused['chat']['status']}，引用{focused['chat']['citation_count']}处。两次结果均保留，不能据此计算准确率。", '',
              '完整结果见 comparison.json，逐条记录见 validation/train/test；实施说明见 ../../docs/phase-twenty-four-reranker.md。']
    (OUT/'B3-report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    lines = ['# 冻结测试 B3 证据变化复核', '', '评分依据仍是待人工复核的候选标签；保留所有退步和改善项。', '']
    for label, key in [('退步', 'test_regressed'), ('改善', 'test_improved')]:
        lines += ['## '+label, '']
        for identifier in comparison[key]:
            row = read(OUT/'test'/(identifier+'.json'))
            lines += [f"- [{identifier}](test/{identifier}.json)：{row['query']}",
                      f"  - B2最终证据Recall={row['B2']['context']['evidence_recall']:.3f}，B3={row['B3']['context']['evidence_recall']:.3f}。",
                      '  - [ ] 核对原文/证据组；[ ] 区分候选截断、分数阈值与排名变化；[ ] 独立复核标签，不改本次冻结数据。']
        lines.append('')
    lines += ['## 真实聊天限制', '',
              '首个广泛问题的模型输出未通过既有证据格式校验，系统澄清并未发布草稿。明确问题的结果单列保存；不丢弃首个结果，不根据它改参数或提示词。']
    (OUT/'failure-review.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def main():
    load_dotenv(ENV_PATH)
    selection, comparison = read(OUT/'selection.json'), read(OUT/'comparison.json')
    assert comparison['completed'] and comparison['selection_sha256'] == sha(OUT/'selection.json')
    assert comparison['profile_file_sha256'] == selection['profile_file_sha256'] == sha(PROFILE)
    assert selection['deployment_profile'] == RerankConfig.deployed().model_dump()
    assert selection['test_labels_used'] is False and comparison['test_used_to_select_parameters'] is False
    async def binding():
        db = configured_database()
        try: return await bind(db)
        finally: await db.close()
    assert asyncio.run(binding()) == selection['binding']
    old = read(OUT/'previous-artifacts.json')
    for name, value in old.items():
        assert sha(PROJECT_ROOT/name) == value, 'Previous evidence/configuration changed: '+name
    pg = read(OUT/'postgresql-verification.json'); assert pg['passed'] and pg['tests'] == 21 and pg['public_tables_unchanged']
    backend = read(OUT/'backend-verification.json'); assert backend['passed']
    live = read(OUT/'live-integration.json'); focused = read(OUT/'live-citation-integration.json')
    assert live['passed'] and focused['passed']
    database = asyncio.run(preserve(live, focused)); worker_check = worker()
    reports(comparison, selection, backend, live, focused)
    review = review_status(PROJECT_ROOT/'.local/eval-v2/reviews.jsonl')
    output = {'passed': True, 'technical_checks_passed': True, 'step': 24,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'dataset': validate(),
              'independent_human_review': review, 'default_retrieval_mode': 'dense',
              'deployment_profile': selection['deployment_profile'], 'deployment_decision_from_validation_only': True,
              'hybrid_B3_final_evidence_improvement_on_test': False,
              'generated_answer_quality_measured': False, 'independent_accuracy_claim': False,
              'backend': backend, 'postgresql_tests': pg, 'worker': worker_check, 'database': database,
              'live_smoke_statuses': [live['chat']['status'], focused['chat']['status']],
              'prior_reports_and_root_env_unchanged': True,
              'selection_sha256': sha(OUT/'selection.json'), 'comparison_sha256': sha(OUT/'comparison.json'),
              'next_step': 25}
    write(OUT/'final-acceptance.json', output)
    print(json.dumps({'passed': True, 'backend_tests': backend['tests_run'], 'skipped': backend['skipped'],
                      'unchanged_old_rows': database['unchanged_old_rows_and_checkpoint_entries'],
                      'default_mode': 'dense', 'live_smokes': output['live_smoke_statuses']}))


if __name__ == '__main__':
    main()
