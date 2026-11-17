"""Focused regression in separate processes; retain prior milestone reports."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from smart_cs.settings import PROJECT_ROOT
from smart_cs.evaluation.v2_assets import validate

OUT = PROJECT_ROOT / '.local/performance'
MODULES = ['test_performance', 'test_models', 'test_observability', 'test_reranking', 'test_grounded_answers', 'test_hybrid_retrieval', 'test_compound_tasks',
           'test_durable_runs', 'test_conversation_persistence', 'test_auth', 'test_frontend_api',
           'test_workflow_api', 'test_approvals', 'test_action_execution', 'test_evaluation_v2', 'test_evaluation_resume']


def child(name, key):
    sys.path.insert(0, str(PROJECT_ROOT/'tests'))
    from contextlib import nullcontext
    manager = nullcontext()
    if name == 'test_durable_runs':
        from test_durable_runs import DurableRunsTests
        original = DurableRunsTests.test_runtime_limit_stops_following_nodes
        async def controlled(self):
            clock = [100.]
            running = self.running
            async def expire_after_entry(job):
                task = await running(job); clock[0] += 2.; return task
            with patch('smart_cs.durable.executor.monotonic', side_effect=lambda: clock[0]):
                with patch.object(self, 'running', new=expire_after_entry):
                    await original(self)
        manager = patch.object(DurableRunsTests, 'test_runtime_limit_stops_following_nodes', new=controlled)
    with manager:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(name))
    record = {'passed': result.wasSuccessful(), 'tests_run': result.testsRun, 'failures': len(result.failures),
              'errors': len(result.errors), 'skipped': len(result.skipped),
              'controlled_clock_for_timeout_test_only': name == 'test_durable_runs'}
    (OUT/'checks'/key/(name+'.json')).write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', choices=MODULES, help=argparse.SUPPRESS)
    parser.add_argument('--key', help=argparse.SUPPRESS); args = parser.parse_args()
    if args.module:
        if not re.fullmatch('[a-f0-9]{32}', args.key or ''): raise ValueError('Invalid check directory')
        return child(args.module, args.key)
    dataset = validate(); key = uuid4().hex; folder = OUT/'checks'/key; folder.mkdir(parents=True)
    modules = {}
    for name in MODULES:
        with (folder/(name+'.log')).open('w', encoding='utf-8') as log:
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--module', name, '--key', key],
                                     cwd=PROJECT_ROOT, stdout=log, stderr=log)
        path = folder/(name+'.json')
        value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'passed': False, 'tests_run': 0, 'skipped': 0, 'errors': 1, 'failures': 0}
        value['process_exit_code'] = process.returncode
        if process.returncode: value['passed'] = False
        modules[name] = value
        print(name+': '+('passed' if value['passed'] else 'FAILED'), flush=True)
    assert validate() == dataset
    report = {'passed': all(r['passed'] for r in modules.values()), 'modules': modules,
              **{k: sum(r[k] for r in modules.values()) for k in ['tests_run', 'skipped', 'errors', 'failures']},
              'module_reports': folder.relative_to(PROJECT_ROOT).as_posix(), 'dataset_hash_unchanged': True,
              'scope': 'Changed retrieval/agent/context paths and dependent auth, compound tasks, approvals, execution, durable recovery and evaluation assets; not a new full-suite run'}
    (OUT/'backend-verification.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
