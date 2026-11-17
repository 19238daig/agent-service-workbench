"""Linux Celery entry point; SQL is the sole result backend."""
import asyncio
import os
import sys
from celery import Celery

app=Celery('smart_cs_jobs',broker=os.environ.get('JOB_BROKER_URL','redis://127.0.0.1:56380/0'))
app.conf.update(task_serializer='json',accept_content=['json'],result_serializer='json',
    task_ignore_result=True,task_acks_late=True,task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,worker_concurrency=1,task_default_queue='smart_cs_jobs',
    task_soft_time_limit=840,task_time_limit=900,broker_transport_options={'visibility_timeout':960},
    broker_connection_retry_on_startup=True,worker_hijack_root_logger=False)

@app.task(name='smart_cs.background.execute',ignore_result=True)
def execute(identifier):
    if sys.platform!='linux':raise RuntimeError('Run this Celery worker in Linux/Docker')
    async def run():
        from smart_cs.db.database import configured_database
        from smart_cs.background.runtime import Worker
        db=configured_database()
        try:return await Worker(db).run(identifier)
        finally:await db.close()
    return asyncio.run(run())
