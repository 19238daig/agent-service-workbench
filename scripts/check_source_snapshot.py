"""Dataset-independent contract checks for the source-only distribution.

Runs without model credentials. Some PostgreSQL-only cases are explicitly skipped;
this is neither a model quality benchmark nor a production acceptance gate.
"""
import compileall
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    'test_models', 'test_dependencies', 'test_action_plans',
    'test_compliance_reviews', 'test_approvals', 'test_action_execution',
    'test_compound_tasks', 'test_delivery', 'test_self_service',
]


def main():
    if not compileall.compile_dir(str(ROOT / 'src'), quiet=1):
        return 1
    env = dict(os.environ)
    # Never consume the caller's live database or model provider credentials.
    for key in list(env):
        if key.endswith(('_API_KEY', '_OPENAI_BASE_URL')) or key in {
            'DATABASE_URL', 'AUTH_VERIFY_SCHEMA', 'MODEL_NAME', 'OPENAI_BASE_URL',
            'JOB_DB_PASSWORD', 'POSTGRES_PASSWORD',
        }:
            env.pop(key)
    env.update(SMART_CS_PROJECT_ROOT=str(ROOT), PYTHONPATH=str(ROOT/'src'),
               PYTHONUTF8='1', RAG_ENABLED='false', REDIS_URL='memory://')
    failed = []
    for module in MODULES:
        result = subprocess.run(
            [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
             '-p', module + '.py', '-v'], cwd=ROOT, env=env,
        )
        if result.returncode:
            failed.append(module)
    print('Source-only contract checks: ' + ('FAILED: '+', '.join(failed) if failed else 'passed'))
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())
