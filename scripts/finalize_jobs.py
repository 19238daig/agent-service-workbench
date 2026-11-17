"""Consolidate step nineteen evidence after API/worker restarts; read-only except login audit/report."""
import asyncio
import hashlib
import json
import subprocess
import time
from datetime import datetime,timezone
import httpx
from dotenv import load_dotenv
from sqlalchemy import select,func,text
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import RetrievalIndex,KnowledgeEmbedding,BackgroundJob
from smart_cs.evaluation.assets import ROOT,CORPUS
from authenticated_client import authenticated_client
from accept_jobs_live import snapshot,compose

def main():
    load_dotenv(ENV_PATH);out=PROJECT_ROOT/'.local/jobs';report={'passed':False,'step':19,'checked_at':datetime.now(timezone.utc).isoformat()}
    try:
        artifacts={key:json.loads(path.read_text(encoding='utf-8')) for key,path in {
            'migration':out/'live-upgrade.json','postgresql':out/'postgresql-verification.json','live':out/'live-acceptance.json','backend':PROJECT_ROOT/'.local/checks/backend.json'}.items()}
        assert all(value['passed'] for value in artifacts.values())
        manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
        for relative,entry in manifest['files'].items():assert hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()==entry['sha256']
        assert hashlib.sha256((CORPUS/'source_manifest.json').read_bytes()).hexdigest()==manifest['source_manifest_sha256']
        for relative,digest in manifest['contract_sha256'].items():assert hashlib.sha256((PROJECT_ROOT/relative).read_bytes()).hexdigest()==digest
        for entry in json.loads((CORPUS/'source_manifest.json').read_text(encoding='utf-8'))['sources']:assert hashlib.sha256((CORPUS/'sources'/entry['file']).read_bytes()).hexdigest()==entry['sha256']
        files=['src/smart_cs/background/'+name for name in ['runtime.py','service.py','handlers.py','files.py','celery_app.py','dispatcher.py']]+['src/smart_cs/schemas/jobs.py','src/smart_cs/db/models.py','src/smart_cs/ingestion/pipeline.py','src/smart_cs/retrieval/indexes.py','src/smart_cs/retrieval/endpoints.py','docs/business-scope.md']
        code="import sys,os,json,hashlib;from pathlib import Path;print(json.dumps({'platform':sys.platform,'uid':os.geteuid(),'sources':{p:hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest() for p in "+repr(files)+"}}))"
        worker=json.loads(compose('exec','-T','worker','python','-c',code))
        assert worker['platform']=='linux' and worker['uid']==10001
        assert all(hashlib.sha256((PROJECT_ROOT/path).read_bytes()).hexdigest()==digest for path,digest in worker['sources'].items())
        pong=json.loads(compose('exec','-T','worker','python','-c',"from smart_cs.background.celery_app import app;import json;print(json.dumps(app.control.ping(timeout=5)))"))
        assert pong and any(v.get('ok')=='pong' for reply in pong for v in reply.values())
        redis=compose('exec','-T','job-redis','redis-cli','CONFIG','GET','maxmemory-policy','appendonly')
        assert 'noeviction' in redis and 'yes' in redis
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=20) as client:
            for _ in range(30):
                try:
                    if client.get('/health').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(1)
            assert client.get('/').status_code==200
            schema=client.get('/openapi.json').json()
            assert schema['paths']['/api/admin/job-artifacts']['post']['requestBody']['content']['application/octet-stream']['schema']['format']=='binary'
            with authenticated_client(client,'admin') as admin:
                for job in artifacts['live']['jobs'].values():
                    row=client.get('/api/admin/jobs/'+job['id']);assert row.status_code==200
                    row=row.json();assert row['status']==job['status'] and row['attempt']==job['attempt'] and row['result']==job['result']
                report['api_restart_results_preserved']=True
        before=json.loads((out/'live-before.json').read_text(encoding='utf-8'));after=asyncio.run(snapshot())
        for name,rows in before.items():
            if name=='checkpoints':assert after[name]==rows
            else:assert all(after[name].get(k)==v for k,v in rows.items()),name
        async def counts():
            db=configured_database()
            try:
                async with db.sessions() as s:
                    active=await s.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
                    revision=await s.scalar(text('SELECT version_num FROM alembic_version'))
                    assert revision=='0013_background_jobs'
                    assert active.id==artifacts['live']['jobs']['index']['result']['previous_index_id']
                    active_vectors=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==active.id))
                    assert active_vectors==547
                    return {'revision':revision,'active_index':active.id,'active_vectors':active_vectors,
                        'staged_index':artifacts['live']['jobs']['index']['result']['index_id'],'staged_vectors':artifacts['live']['jobs']['index']['result']['chunks']}
            finally:await db.close()
        report.update(passed=True,backend={k:v for k,v in artifacts['backend'].items() if k not in {'modules','module_reports'}},
            postgresql=artifacts['postgresql'],live={k:v for k,v in artifacts['live'].items() if k not in {'jobs','frozen_assets'}},
            runtime=asyncio.run(counts()),linux_nonroot_worker=True,worker_ping=True,dedicated_redis_aof_noeviction=True,
            worker_code_matches_host=True,original_rows_and_checkpoints_preserved=True,frozen_assets_preserved=True,
            input_formats_tested=['pdf','html'],worker_source_sha256=worker['sources'],
            evidence_files=['.local/jobs/live-upgrade.json','.local/jobs/postgresql-verification.json','.local/jobs/live-acceptance.json','.local/checks/backend.json'],
            next_step=20,react_management_page_step=21)
    except Exception as error:report.update(passed=False,error_type=type(error).__name__)
    (out/'final-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'worker_source_sha256','postgresql','live'}},ensure_ascii=False,indent=2));return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
