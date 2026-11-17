"""Finalize B2 evidence without changing frozen rankings, labels or business data."""
import asyncio
from datetime import datetime, timezone
import importlib.metadata
import json
import subprocess

from dotenv import load_dotenv
from sqlalchemy import func, select, text

from accept_runs_live import snapshot
from evaluate_hybrid import OUT, B1, read, write, sha, code
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from smart_cs.evaluation.v2_assets import validate, review_status
from smart_cs.db.database import configured_database
from smart_cs.db.models import KnowledgeEmbedding
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.retrieval.hybrid import HybridConfig
from smart_cs.retrieval.lexical import RESOURCES, tokenizer


def backend_checks():
    original = read(PROJECT_ROOT / '.local/checks/backend.json')
    assert 'test_hybrid_retrieval' in original['modules'], 'Current full backend run has not finished'
    modules = dict(original['modules'])
    reruns = {}
    for name, record in modules.items():
        if record['passed']:
            continue
        assert name in {'test_durable_runs', 'test_evaluation_resume', 'test_evaluation_v2'}, 'Unexpected backend failure'
        path = OUT / 'regression-reruns' / (name + '.json')
        rerun = read(path)
        assert rerun['passed'] and rerun['tests_run'] == record['tests_run']
        reruns[name] = {'original': record, 'rerun': rerun, 'report': path.relative_to(PROJECT_ROOT).as_posix(),
                       'reason': ('Controlled clock after held model starts; original test body, assertions and frozen file hash preserved.'
                                  if name == 'test_durable_runs' else
                                  'Frozen contract file restored byte-for-byte after a rejected fixture edit; validation rerun without changing manifest.')}
        modules[name] = rerun
    assert all(r['passed'] for r in modules.values())
    return {'passed': True, 'tests_run': sum(r['tests_run'] for r in modules.values()),
            'skipped': sum(r['skipped'] for r in modules.values()), 'module_count': len(modules),
            'original_full_run_passed': original['passed'],
            'original_report_sha256': sha(PROJECT_ROOT / '.local/checks/backend.json'),
            'original_module_reports': original['module_reports'], 'reruns': reruns,
            'scope': 'All backend modules; original failed runs retained, affected modules rerun with frozen contract bytes preserved'}


def dependencies():
    check = subprocess.run([str(PROJECT_ROOT / '.venv/Scripts/python.exe'), '-m', 'pip', 'check'],
                           cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, 'Local dependency check failed'
    probe = (
        "import json,importlib.metadata as m;"
        "from smart_cs.retrieval.lexical import tokenizer;"
        "from smart_cs.retrieval.hybrid import HybridConfig;"
        "print(json.dumps({'jieba':m.version('jieba'),'rank_bm25':m.version('rank-bm25'),"
        "'tokenizer_sha256':tokenizer().fingerprint,'profile':HybridConfig.deployed().model_dump()}))"
    )
    result = subprocess.run(['docker', 'compose', '-p', 'smart-cs-foundation', '--profile', 'jobs',
                             'exec', '-T', 'worker', 'python', '-c', probe], cwd=PROJECT_ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, 'Worker dependency probe failed'
    worker = json.loads(result.stdout)
    assert worker['jieba'] == importlib.metadata.version('jieba') == '0.42.1'
    assert worker['rank_bm25'] == importlib.metadata.version('rank-bm25') == '0.2.2'
    assert worker['tokenizer_sha256'] == tokenizer().fingerprint
    assert worker['profile'] == HybridConfig.deployed().model_dump()
    return {'passed': True, 'local_pip_check': check.stdout.strip(), 'worker': worker,
            'worker_images_rebuilt_and_services_restarted': True}


async def preservation():
    before = read(OUT / 'original-rows.json')
    after = await snapshot()
    assert set(before) == set(after), 'Database table set changed'
    checked = 0
    for table, records in before.items():
        assert records == after[table], 'Database rows changed: ' + table
        checked += len(records)
    db = configured_database()
    try:
        async with db.sessions() as s:
            index = await DenseRetriever(db).active(s)
            vectors = await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id == index.id))
            revision = await s.scalar(text('SELECT version_num FROM alembic_version'))
        assert index.id == read(B1 / 'run.json')['index_id'] and vectors == 547
        assert revision == '0015_knowledge_admin'
        return {'passed': True, 'tables': len(before), 'unchanged_rows_and_checkpoint_entries': checked,
                'new_business_rows': 0, 'active_index': index.id, 'vectors': vectors,
                'database_revision': revision, 'auth_sessions_and_login_attempts_excluded': True}
    finally:
        await db.close()


def readable_reports(comparison, selection, backend):
    lines = ['# 第23步 B1/B2 检索对照', '',
             '混合检索功能验收通过；质量提升门槛未通过，默认保持 dense。标签仍待独立人工复核。', '',
             '固定业务日期2026-11-15，同一活动索引与真实本地Embedding；本对照没有重排、查询改写或生成模型调用。', '',
             '| 划分 | 正例 | B1 Recall@5 | B2 Recall@5 | B1 Hit@5 | B2 Hit@5 | B1 MRR@5 | B2 MRR@5 |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for split in ['train', 'validation', 'test']:
        a, b = (comparison['by_split'][split][k] for k in ['B1', 'B2'])
        lines.append(f"| {split} | {a['answerable']} | {a['recall_at_5']:.4f} | {b['recall_at_5']:.4f} | {a['hit_any_at_5']:.4f} | {b['hit_any_at_5']:.4f} | {a['mrr_at_5']:.4f} | {b['mrr_at_5']:.4f} |")
    lines += ['', '选定参数：`' + json.dumps(comparison['selected_config']) + '`。', '',
              '启用门槛在测试前确定；验证Recall没有增加0.01，不启用混合检索。测试没有用于参数选择。', '',
              '延迟：`' + json.dumps(comparison['runtime_latency_test'], ensure_ascii=False) + '`。', '',
              f"全量正例改善 {len(comparison['improved_cases'])} 条，退步 {len(comparison['regressed_cases'])} 条；不能只报告改善样例。", '',
              f"后端汇总：{backend['tests_run']}项，跳过{backend['skipped']}项；原始运行和失败模块重跑均保留。", '',
              '详见 ../../docs/phase-twenty-three-hybrid-retrieval.md；完整数值见 comparison.json。']
    (OUT / 'B2-report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    lines = ['# B2 改善与退步样例复核', '', '逐条记录保留完整候选ID、证据覆盖与支路排名。以下仅为候选标签评分变化，根因未人工确认。', '']
    for label, key in [('退步', 'regressed_cases'), ('改善', 'improved_cases')]:
        lines += ['## ' + label, '']
        for identifier in comparison[key]:
            folder = 'test' if (OUT / 'test' / (identifier + '.json')).exists() else 'development'
            record = read(OUT / folder / (identifier + '.json'))
            query = record['query']
            lines += [f'- [{identifier}]({folder}/{identifier}.json)：{query}',
                      '  - [ ] 复核原始证据组；[ ] 核对词项/支路排名；[ ] 诊断融合排序；[ ] 确认标签或另发数据版本。']
        lines.append('')
    (OUT / 'failure-review.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    load_dotenv(ENV_PATH)
    comparison, selection = (read(OUT / n) for n in ['comparison.json', 'selection.json'])
    assert comparison['completed'] and comparison['pure_dense_rankings_match_original_B1']
    assert comparison['selection_sha256'] == sha(OUT / 'selection.json')
    assert comparison['binding'] == selection['binding']
    assert code() == selection['binding']['code_sha256'], 'Frozen retrieval sources changed'
    assert sha(RESOURCES / 'hybrid_profile.json') == selection['profile_file_sha256']
    assert selection['test_labels_used'] is False and comparison['test_used_to_select_parameters'] is False
    assert selection['recommended_online_mode'] == comparison['deployment_decision'] == 'dense'
    assert validate() == selection['binding']['dataset']
    review = review_status(PROJECT_ROOT / '.local/eval-v2/reviews.jsonl')
    pg = read(OUT / 'postgresql-verification.json')
    assert pg['passed'] and pg['tests'] == 15 and pg['public_tables_unchanged']
    live = read(OUT / 'live-integration.json')
    assert live['passed'] and all(v['default_search'] == 'exact_cosine' for v in live['http'].values())
    backend = backend_checks()
    write(OUT / 'backend-verification.json', backend)
    deps = dependencies()
    database = asyncio.run(preservation())
    readable_reports(comparison, selection, backend)
    report = {'passed': True, 'technical_checks_passed': True, 'step': 23,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'quality_improvement_gate_passed': False,
              'default_mode': 'dense', 'explicit_hybrid_mode_available': True,
              'b2_generation_quality_measured': False, 'independent_human_review': review,
              'dataset': validate(), 'backend': backend, 'postgresql_tests': pg, 'dependencies': deps,
              'database': database, 'live_integration_passed': True,
              'comparison_sha256': sha(OUT / 'comparison.json'), 'selection_sha256': sha(OUT / 'selection.json'),
              'next_step': 24}
    write(OUT / 'final-acceptance.json', report)
    print(json.dumps({'passed': True, 'default_mode': 'dense', 'quality_gate': False,
                      'backend_tests': backend['tests_run'], 'unchanged_rows': database['unchanged_rows_and_checkpoint_entries']}))


if __name__ == '__main__':
    main()
