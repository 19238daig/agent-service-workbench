"""Start previously built Linux workers without downloading/building on double click."""
import os
import subprocess
from smart_cs.settings import PROJECT_ROOT

def ensure_jobs():
    if not os.getenv('JOB_DB_PASSWORD'):raise RuntimeError('Configure the dedicated worker role first')
    result=subprocess.run(['docker','compose','--profile','jobs','up','-d','--no-build','--wait','--wait-timeout','45','job-redis','worker','job-dispatcher'],
        cwd=PROJECT_ROOT,capture_output=True,timeout=60)
    if result.returncode:raise RuntimeError('Worker not ready; build Dockerfile.worker and inspect local logs')
