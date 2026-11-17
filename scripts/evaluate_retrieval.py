"""Evaluate the lexical bootstrap on held-out synthetic RAG questions only."""
import json
import time
from statistics import mean

from smart_cs.datasets import DATA_ROOT, validate_assets
from smart_cs.memory.lexical_knowledge import LexicalKnowledgeMemory
from smart_cs.settings import PROJECT_ROOT

validation = validate_assets()
documents = json.loads((DATA_ROOT / 'knowledge/documents.json').read_text(encoding='utf-8'))
by_source = {d['source']: d['id'] for d in documents}
memory = LexicalKnowledgeMemory(documents)
samples = [json.loads(line) for line in (DATA_ROOT / 'rag/test.jsonl').read_text(encoding='utf-8').splitlines()]
rows = []
for sample in samples:
    started = time.perf_counter()
    retrieved = [by_source[d['source']] for d in memory.search(sample['question'], top_k=5)]
    references = set(sample['reference_document_ids'])
    ranks = [i+1 for i, identifier in enumerate(retrieved) if identifier in references]
    rows.append({'id': sample['id'], 'retrieved': retrieved, 'reference': sorted(references),
                 'hit_at_5': bool(ranks), 'recall_at_5': len(set(retrieved) & references) / len(references),
                 'reciprocal_rank_at_5': 1/min(ranks) if ranks else 0,
                 'duration_ms': (time.perf_counter()-started)*1000})
report = {'dataset_version': validation['release_id'], 'manifest_sha256': validation['manifest_sha256'],
          'mode': memory.retrieval_mode, 'split': 'test', 'samples': len(rows), 'human_reviewed': False,
          'hit_at_5': mean(r['hit_at_5'] for r in rows), 'recall_at_5': mean(r['recall_at_5'] for r in rows),
          'mrr_at_5': mean(r['reciprocal_rank_at_5'] for r in rows), 'results': rows,
          'limitations': ['Small closed synthetic corpus; not real-world retrieval quality',
                         'No generative answer or unsupported-question evaluation', 'Not semantic embedding retrieval']}
output = PROJECT_ROOT / '.local/evaluation'
output.mkdir(parents=True, exist_ok=True)
(output / 'retrieval-baseline.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({k: report[k] for k in ['mode','samples','hit_at_5','recall_at_5','mrr_at_5','human_reviewed']}, ensure_ascii=False))
