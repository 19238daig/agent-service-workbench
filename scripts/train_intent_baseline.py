"""Train a small supervised baseline on synthetic data, not an LLM fine-tune.

The live four-agent service keeps its configured model routing. No trained
artifact from this script is automatically trusted or deployed by the API.
"""
from pathlib import Path
import json
import sys
from collections import Counter

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, f1_score
from sklearn.pipeline import Pipeline
import joblib

from smart_cs.datasets import DATA_ROOT, sha, validate_assets
from smart_cs.settings import PROJECT_ROOT


def read(split):
    return [json.loads(line) for line in (DATA_ROOT / 'intent' / f'{split}.jsonl').read_text(encoding='utf-8').splitlines()]


def main():
    validation = validate_assets()
    train, dev, test = (read(split) for split in ['train', 'validation', 'test'])
    candidates = []
    models = {}
    for regularization in [0.5, 2.0, 8.0]:
        model = Pipeline([('tfidf', TfidfVectorizer(analyzer='char', ngram_range=(2, 4), sublinear_tf=True)),
                          ('classifier', LogisticRegression(C=regularization, max_iter=1500, random_state=20261113))])
        model.fit([s['text'] for s in train], [s['label'] for s in train])
        predicted = model.predict([s['text'] for s in dev])
        score = f1_score([s['label'] for s in dev], predicted, average='macro', zero_division=0)
        candidates.append({'C': regularization, 'validation_macro_f1': score})
        models[regularization] = model
    selected = max(candidates, key=lambda c: (c['validation_macro_f1'], -c['C']))
    model = models[selected['C']]
    # Test labels are used only after the validation-only model selection.
    predicted = model.predict([s['text'] for s in test])
    expected = [s['label'] for s in test]
    report = {'task': 'synthetic_intent_supervised_baseline', 'algorithm': 'char TF-IDF + LogisticRegression',
              'dataset_version': validation['release_id'], 'manifest_sha256': validation['manifest_sha256'],
              'counts': {'train': len(train), 'validation': len(dev), 'test': len(test)},
              'selection': candidates, 'selected_C': selected['C'],
              'test_accuracy': accuracy_score(expected, predicted),
              'test_macro_f1': f1_score(expected, predicted, average='macro', zero_division=0),
              'per_class': classification_report(expected, predicted, output_dict=True, zero_division=0),
              'label_distribution': dict(Counter(expected)), 'human_reviewed': False,
              'limitations': ['AI-authored synthetic templates; not independent real customer data',
                  'exact/group split checks do not prove absence of semantic similarity leakage',
                  'includes planned multi-intent behavior; not a live-agent success metric',
                  'not an LLM fine-tune; not deployed into the chat service']}
    output = PROJECT_ROOT / '.local' / 'training'
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output / 'intent-baseline.joblib')
    report['artifact_sha256'] = sha((output / 'intent-baseline.joblib').read_bytes())
    (output / 'intent-baseline-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'intent-test-predictions.jsonl').write_text(''.join(json.dumps({'id': sample['id'], 'expected': truth, 'predicted': guess}, ensure_ascii=False) + '\n' for sample, truth, guess in zip(test, expected, predicted)), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ['algorithm','counts','test_accuracy','test_macro_f1','human_reviewed']}, ensure_ascii=False))
    print('Report: .local/training/intent-baseline-report.json (synthetic data only; not live agent quality).')


if __name__ == '__main__':
    main()
