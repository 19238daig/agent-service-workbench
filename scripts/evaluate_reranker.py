"""Step 24: predeclared validation selection, frozen B2/B3 + dense controls.

No chat calls, rewrites or label indexing. Generation quality is not measured.
"""
import argparse
import asyncio
from datetime import date, datetime, timezone
import itertools
import hashlib
import json
import os
from pathlib import Path
from statistics import mean
from time import perf_counter

from dotenv import load_dotenv
import httpx
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.evaluation.v2_assets import validate, rows, encoded
from smart_cs.evaluation.v2_schema import RagV2
from smart_cs.evaluation.runner import evidence_scores
from smart_cs.evaluation.v2_metrics import percentiles
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.retrieval.hybrid import HybridRetriever, HybridConfig
from smart_cs.retrieval.reranking import RerankConfig, CrossEncoderReranker, PROFILE, digest, validate_identity
from evaluate_hybrid import read, write, sha

OUT = PROJECT_ROOT / '.local/reranker'
B2 = PROJECT_ROOT / '.local/hybrid'
B1 = PROJECT_ROOT / '.local/evaluation/v2-b1-20261116-resume'
DAY = date(2026, 11, 15)


def legacy():
    return RerankConfig.model_validate(read(OUT/'legacy-profile.json'))


def grid():
    base = legacy()
    return [base.model_copy(update={'candidate_k': n, 'context_k': k, 'max_length': length})
            for n, k, length in itertools.product([10, 20], [3, 5], [256, 512])]


def code():
    paths = [Path(__file__).resolve(), PROJECT_ROOT/'scripts/local_reranker_server.py',
             PROJECT_ROOT/'src/smart_cs/memory/bge_memory.py', PROJECT_ROOT/'src/smart_cs/agents/knowledge_rag.py',
             PROJECT_ROOT/'src/smart_cs/persistence/conversations.py', PROJECT_ROOT/'requirements.lock.txt',
             *sorted((PROJECT_ROOT/'src/smart_cs/retrieval').glob('*.py')),
             *sorted((PROJECT_ROOT/'src/smart_cs/retrieval/resources').glob('*.txt')),
             PROJECT_ROOT/'src/smart_cs/retrieval/resources/hybrid_profile.json']
    return {p.relative_to(PROJECT_ROOT).as_posix(): sha(p) for p in paths}


async def bind(db):
    old = read(B1/'run.json'); previous = read(B2/'comparison.json')
    dataset = validate()
    assert dataset == old['dataset'] == previous['binding']['dataset']
    for name, h in previous['binding']['code_sha256'].items():
        assert sha(PROJECT_ROOT/name) == h, 'Frozen B2 implementation changed: '+name
    dense = DenseRetriever(db, clock=lambda: DAY)
    async with db.sessions() as s:
        index = await dense.active(s)
    assert index.id == old['index_id']
    assert hashlib.sha256(encoded(index.configuration)).hexdigest() == old['index_configuration_sha256']
    assert await dense.adapter.aidentity() == index.configuration['embedding_contract']
    cfg = legacy(); assert float(os.getenv('RAG_RERANK_MIN_SCORE', '.1')) == cfg.min_score
    async with httpx.AsyncClient(trust_env=False, timeout=20) as c:
        response = await c.get(os.getenv('RAG_RERANKER_URL', 'http://127.0.0.1:61536')+'/identity')
        response.raise_for_status(); identity = validate_identity(response.json(), cfg.expected_identity)
    return {'dataset': dataset, 'index_id': index.id, 'index_configuration_sha256': old['index_configuration_sha256'],
            'embedding': index.model_identity, 'business_date': DAY.isoformat(), 'reranker_identity': identity,
            'code_sha256': code(), 'B2_selection_sha256': sha(B2/'selection.json'),
            'B2_comparison_sha256': sha(B2/'comparison.json'), 'legacy_profile': cfg.model_dump(),
            'chat_calls': 0, 'rewrite': False, 'labels_indexed': False, 'human_gold': False}


def measure(case, ranked, context):
    positive = case.answerable
    return {'id': case.id, 'split': case.split, 'group_id': case.group_id, 'answerable': positive,
            'ranking_at_3': evidence_scores(case, ranked, 3) if positive else None,
            'ranking_at_5': evidence_scores(case, ranked, 5) if positive else None,
            'context': evidence_scores(case, context, max(1, len(context))) if positive else None,
            'ranked_ids': [d['id'] for d in ranked], 'context_ids': [d['id'] for d in context],
            'context_count': len(context), 'context_characters': sum(len(d['content']) for d in context)}


def summary(items):
    positive = [r for r in items if r['answerable']]; groups = {}
    for r in positive:
        groups.setdefault(r['group_id'], []).append(r['context']['evidence_recall'])
    return {'cases': len(items), 'positive_cases': len(positive), 'positive_groups': len(groups),
            'ranking_recall_at_3': mean(r['ranking_at_3']['evidence_recall'] for r in positive),
            'ranking_recall_at_5': mean(r['ranking_at_5']['evidence_recall'] for r in positive),
            'ranking_hit_at_5': mean(r['ranking_at_5']['hit_any'] for r in positive),
            'ranking_mrr_at_5': mean(r['ranking_at_5']['reciprocal_rank'] for r in positive),
            'context_recall': mean(r['context']['evidence_recall'] for r in positive),
            'context_hit': mean(r['context']['hit_any'] for r in positive),
            'context_mrr': mean(r['context']['reciprocal_rank'] for r in positive),
            'group_context_recall': mean(mean(values) for values in groups.values()),
            'mean_context_characters': mean(r['context_characters'] for r in items),
            'mean_context_count': mean(r['context_count'] for r in items),
            'negative_empty_context_cases': sum(not r['answerable'] and not r['context_ids'] for r in items),
            'negative_cases': sum(not r['answerable'] for r in items)}


def from_scores(case, pool, result, config):
    candidates = pool[:config.candidate_k]
    if config.enabled:
        ranked = sorted([{**d, 'rerank_score': s} for d, s in zip(candidates, result['scores'])], key=lambda d: (-d['rerank_score'], d['id']))
        context = [d for d in ranked if d['rerank_score'] >= config.min_score][:config.context_k]
    else:
        ranked = candidates; context = ranked[:config.context_k]
    return measure(case, ranked, context)


async def sources(db, case):
    hybrid = HybridRetriever(db, clock=lambda: DAY, config=HybridConfig.model_validate(read(B2/'selection.json')['winner']['config']))
    prepared = await hybrid.prepare(case.message)
    dense = prepared.documents[:20]; fused, _ = prepared.rank(case.message, hybrid.config, 20)
    old_dense = read(B1/'retrieval'/(case.id+'.json'))['documents']
    assert [d['id'] for d in dense] == [d['id'] for d in old_dense]
    if case.split == 'test':
        ids = read(B2/'test'/(case.id+'.json'))['B2']['ids']
    else:
        ids = read(B2/'development'/(case.id+'.json'))['profiles'][read(B2/'selection.json')['winner']['config_sha256']]['ids']
    assert [d['id'] for d in fused] == ids, 'B2 candidate order drift'
    return {'dense': dense, 'hybrid': fused}, hybrid.dense


async def score_pool(query, pool, config, dense):
    documents = pool[:config.candidate_k]
    await dense.revalidate(documents)
    started = perf_counter()
    result = await CrossEncoderReranker(config).score(query, [(d['metadata'].get('heading') or '')+'\n'+d['content'] for d in documents])
    elapsed = (perf_counter()-started)*1000
    await dense.revalidate(documents)
    return {**result, 'client_duration_ms': elapsed}


def improved(candidate, baseline):
    return (candidate['context_recall'] >= baseline['context_recall']+.01
            and candidate['group_context_recall'] >= baseline['group_context_recall']
            and candidate['context_hit'] >= baseline['context_hit']
            and candidate['context_mrr'] >= baseline['context_mrr']-.02)


async def tune():
    assert not (OUT/'selection.json').exists(), 'Parameters already frozen; do not tune on test'
    db = configured_database()
    try:
        binding = await bind(db); configs = grid(); base = legacy()
        bypass = [base.model_copy(update={'enabled': False, 'context_k': k}) for k in [3, 5]]
        protocol = {'binding': binding, 'grid': [c.model_dump() for c in configs], 'bypass_controls': [c.model_dump() for c in bypass],
                    'selection_splits': ['validation'], 'train_measured_after_selection': True, 'test_labels_used': False,
                    'objective': 'group context recall, context recall, context MRR, lower evidence/candidate/token budgets',
                    'deployment_gate': 'dense validation context recall gain>=.01 vs legacy; group recall/hit no loss; MRR loss<=.02; rerank p95<=1500ms; otherwise preserve legacy',
                    'hybrid_mode': 'remains opt-in; B3 measured against frozen B2 at matched evidence budget',
                    'threshold': 'existing root .env value fixed, not tuned; normalized score is not confidence'}
        if (OUT/'development-run.json').exists():
            assert read(OUT/'development-run.json') == protocol, 'Experiment inputs changed'
        else: write(OUT/'development-run.json', protocol)
        cases = [r for r in rows() if isinstance(r, RagV2) and r.evaluation_scope == 'retrieval' and r.split == 'validation']
        records = []
        for i, case in enumerate(cases):
            path = OUT/'validation'/(case.id+'.json')
            if path.exists(): records.append(read(path)); continue
            pools, dense = await sources(db, case); scores = {}; variants = {}
            for cfg in configs:
                key = f'{cfg.candidate_k}-{cfg.max_length}'
                if key not in scores:
                    scores[key] = {mode: await score_pool(case.message, pool, cfg, dense) for mode, pool in pools.items()}
                variants[digest(cfg.model_dump())] = {mode: from_scores(case, pool, scores[key][mode], cfg) for mode, pool in pools.items()}
            for cfg in bypass:
                variants[digest(cfg.model_dump())] = {mode: from_scores(case, pool, None, cfg) for mode, pool in pools.items()}
            record = {'id': case.id, 'query': case.message, 'pools': pools, 'scores': scores, 'variants': variants}
            write(path, record); records.append(record)
            print(json.dumps({'phase': 'validation', 'completed': i+1, 'total': len(cases)}), flush=True)
        tables = {}
        for mode, candidates in [('hybrid', configs), ('dense', configs+bypass)]:
            table = []
            for cfg in candidates:
                key = digest(cfg.model_dump()); metric = summary([r['variants'][key][mode] for r in records])
                latency = percentiles([r['scores'][f'{cfg.candidate_k}-{cfg.max_length}'][mode]['client_duration_ms'] for r in records]) if cfg.enabled else {'p95': 0.}
                table.append({'config': cfg.model_dump(), 'key': key, 'metrics': metric, 'score_latency_ms': latency})
            table.sort(key=lambda r: (r['metrics']['group_context_recall'], r['metrics']['context_recall'], r['metrics']['context_mrr'],
                                     -r['config']['context_k'], -r['config']['candidate_k'], -r['config']['max_length'], not r['config']['enabled']), reverse=True)
            tables[mode] = table
        dense_best, hybrid_best = tables['dense'][0], tables['hybrid'][0]
        old = next(r for r in tables['dense'] if r['config'] == base.model_dump())
        change = improved(dense_best['metrics'], old['metrics']) and dense_best['score_latency_ms']['p95'] <= 1500
        deployment = dense_best['config'] if change else base.model_dump()
        write(PROFILE, deployment)
        selection = {'binding': binding, 'frozen_at': datetime.now(timezone.utc).isoformat(), 'test_labels_used': False,
                     'B3_hybrid': hybrid_best, 'dense_best': dense_best, 'legacy_dense': old, 'profiles': tables,
                     'deployment_profile': deployment, 'deployment_changed_from_legacy': change,
                     'default_retrieval_mode': 'dense', 'profile_file_sha256': sha(PROFILE)}
        assert await bind(db) == binding
        write(OUT/'selection.json', selection)
        print(json.dumps({'parameters_frozen': True, 'B3': hybrid_best['config'], 'online': deployment,
                          'online_changed': change}, ensure_ascii=False), flush=True)
        return selection
    finally:
        await db.close()


async def compare():
    assert not (OUT/'comparison.json').exists(), 'Frozen comparison already exists'
    selection = read(OUT/'selection.json'); db = configured_database()
    try:
        assert await bind(db) == selection['binding']
        assert sha(PROFILE) == selection['profile_file_sha256']
        cfg = RerankConfig.model_validate(selection['B3_hybrid']['config'])
        online = RerankConfig.model_validate(selection['deployment_profile']); base = legacy()
        training = []
        for case in [r for r in rows() if isinstance(r, RagV2) and r.evaluation_scope == 'retrieval' and r.split == 'train']:
            path = OUT/'train'/(case.id+'.json')
            if path.exists(): training.append(read(path)); continue
            pools, dense = await sources(db, case); values = {}; audits = {}
            for name, mode, profile in [('B3', 'hybrid', cfg), ('online_dense', 'dense', online)]:
                if profile.enabled:
                    result = await score_pool(case.message, pools[mode], profile, dense); audits[name] = result
                else: result = None
                values[name] = from_scores(case, pools[mode], result, profile)
            record = {'id': case.id, 'query': case.message, **values, 'B2': measure(case, pools['hybrid'], pools['hybrid'][:cfg.context_k]),
                      'dense_plain': measure(case, pools['dense'], pools['dense'][:online.context_k]), 'audits': audits}
            write(path, record); training.append(record)
            if len(training)%20 == 0: print(json.dumps({'phase': 'train_fixed_profile', 'completed': len(training), 'total': 172}), flush=True)
        test = []
        cases = [r for r in rows() if isinstance(r, RagV2) and r.evaluation_scope == 'retrieval' and r.split == 'test']
        hybrid = HybridRetriever(db, clock=lambda: DAY); dense = DenseRetriever(db, clock=lambda: DAY)
        for i, case in enumerate(cases):
            path = OUT/'test'/(case.id+'.json')
            if path.exists(): test.append(read(path)); continue
            results, times, audits = {}, {}, {}
            modes = ['B2', 'B3', 'legacy_dense', 'online_dense']
            if i%2: modes.reverse()
            for name in modes:
                start = perf_counter()
                if name in {'B2', 'B3'}:
                    response = await hybrid.search(case.message, 20)
                    assert [d['id'] for d in response['documents']] == read(B2/'test'/(case.id+'.json'))['B2']['ids']
                    if name == 'B2':
                        value = measure(case, response['documents'], response['documents'][:cfg.context_k])
                    else:
                        ranked = await CrossEncoderReranker(cfg).rank(case.message, response['documents'], revalidate=dense.revalidate)
                        value = measure(case, ranked['ranked_candidates'], ranked['documents']); audits[name] = ranked['audit']
                else:
                    profile = base if name == 'legacy_dense' else online
                    response = await dense.search(case.message, profile.candidate_k)
                    assert [d['id'] for d in response['documents']] == [d['id'] for d in read(B1/'retrieval'/(case.id+'.json'))['documents'][:profile.candidate_k]]
                    ranked = await CrossEncoderReranker(profile).rank(case.message, response['documents'], revalidate=dense.revalidate)
                    value = measure(case, ranked['ranked_candidates'], ranked['documents']); audits[name] = ranked['audit']
                times[name] = (perf_counter()-start)*1000; results[name] = value
            record = {'id': case.id, 'query': case.message, **results, 'duration_ms': times, 'audits': audits}
            write(path, record); test.append(record)
            if (i+1)%8 == 0: print(json.dumps({'phase': 'frozen_test', 'completed': i+1, 'total': len(cases)}), flush=True)
        validation = [read(p) for p in sorted((OUT/'validation').glob('*.json'))]
        def val_rows(name):
            profile = cfg if name == 'B3' else online
            mode = 'hybrid' if name == 'B3' else 'dense'
            return [r['variants'][digest(profile.model_dump())][mode] for r in validation]
        report = {'completed': True, 'binding': selection['binding'], 'selection_sha256': sha(OUT/'selection.json'),
                  'profile_file_sha256': sha(PROFILE), 'default_retrieval_mode': 'dense',
                  'deployment_changed_from_legacy': selection['deployment_changed_from_legacy'],
                  'test_used_to_select_parameters': False,
                  'validation': {'B3': summary(val_rows('B3')), 'online_dense': summary(val_rows('online_dense'))},
                  'train': {k: summary([r[k] for r in training]) for k in ['B2', 'B3', 'dense_plain', 'online_dense']},
                  'test': {k: summary([r[k] for r in test]) for k in ['B2', 'B3', 'legacy_dense', 'online_dense']},
                  'test_latency_ms': {k: percentiles([r['duration_ms'][k] for r in test]) for k in ['B2', 'B3', 'legacy_dense', 'online_dense']},
                  'B3_paired_added_latency_ms': percentiles([r['duration_ms']['B3']-r['duration_ms']['B2'] for r in test]),
                  'B3_rerank_ms': percentiles([r['audits']['B3']['duration_ms'] for r in test]),
                  'test_improved': [r['id'] for r in test if r['B3']['answerable'] and r['B3']['context']['evidence_recall']>r['B2']['context']['evidence_recall']],
                  'test_regressed': [r['id'] for r in test if r['B3']['answerable'] and r['B3']['context']['evidence_recall']<r['B2']['context']['evidence_recall']],
                  'truncated_pairs_test_B3': sum(r['audits']['B3']['truncated_pairs'] for r in test),
                  'latency_method': 'same process, warm GPU/tokenizer, reverse whole pipeline ordering on alternating queries; no result cache',
                  'limitations': ['Candidate labels pending independent human review.', 'Validation positives: 12 cases / 5 groups.',
                                  'Retrieval/evidence component only; no generated-answer quality, calibrated refusal or end-to-end success claim.',
                                  'Larger final evidence budgets change context size; matched B2 budget and dense controls reported separately.',
                                  'Embedding server cache and machine load uncontrolled; not production throughput.']}
        assert await bind(db) == selection['binding']
        write(OUT/'comparison.json', report)
        return report
    finally:
        await db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('action', choices=['tune', 'compare'])
    args = p.parse_args(); load_dotenv(ENV_PATH); OUT.mkdir(exist_ok=True)
    asyncio.run(tune() if args.action == 'tune' else compare())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
