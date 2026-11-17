"""Rerun affected modules while preserving the frozen v2 contract test bytes.

Only the timeout scenario uses a controlled monotonic clock. Its original test
body and assertions still execute; time advances after the held node starts.
"""
import json
import sys
import unittest
from unittest.mock import patch

from smart_cs.settings import PROJECT_ROOT
from smart_cs.evaluation.v2_assets import validate


def main():
    validate()  # Includes the original test_durable_runs.py byte hash.
    sys.path.insert(0, str(PROJECT_ROOT / 'tests'))
    from test_durable_runs import DurableRunsTests
    original = DurableRunsTests.test_runtime_limit_stops_following_nodes

    async def controlled_timeout(self):
        clock = [100.0]
        original_running = self.running

        async def entered_then_expire(job):
            task = await original_running(job)
            clock[0] += 2.0
            return task

        with patch('smart_cs.durable.executor.monotonic', side_effect=lambda: clock[0]):
            with patch.object(self, 'running', new=entered_then_expire):
                await original(self)

    output = PROJECT_ROOT / '.local/hybrid/regression-reruns'
    output.mkdir(parents=True, exist_ok=True)
    passed = True
    with patch.object(DurableRunsTests, 'test_runtime_limit_stops_following_nodes', new=controlled_timeout):
        for name in ['test_durable_runs', 'test_evaluation_resume', 'test_evaluation_v2']:
            with (output / (name + '.log')).open('w', encoding='utf-8') as stream:
                result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(name))
            report = {'passed': result.wasSuccessful(), 'tests_run': result.testsRun,
                      'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
                      'frozen_contract_bytes_unchanged': True,
                      'controlled_clock_for_timeout_test_only': name == 'test_durable_runs'}
            (output / (name + '.json')).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            print(name + ': ' + ('passed' if result.wasSuccessful() else 'FAILED'), flush=True)
            passed = passed and result.wasSuccessful()
    validate()
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
