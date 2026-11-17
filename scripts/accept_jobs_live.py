"""Real Linux Celery acceptance: authentic PDFs/HTML, BGE, kill/restart and SQL results.

Creates explicitly private admin QA imports and one inactive index. Does not alter
original knowledge, active index, orders, tickets, frozen labels or checkpoints.
"""
import asyncio
import hashlib
import json
import subprocess
import time
from datetime import datetime,timezone
from pathlib import Path
import httpx
from dotenv import load_dotenv
from sqlalchemy import text
from smart_cs.db.models import Base
from smart_cs.db.database import configured_database
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.evaluation.assets import validate,ROOT,CORPUS
from authenticated_client import authenticated_client

OUT=PROJECT_ROOT/'.local/jobs'
def compose(*args):
    result=subprocess.run(['docker','compose','--profile','jobs',*args],cwd=PROJECT_ROOT,capture_output=True,timeout=120)
    if result.returncode:raise RuntimeError('Compose operation failed; inspect local Docker logs')
    return result.stdout.decode(errors='replace')

async def snapshot():
    db=configured_database();result={}
    try:
        async with db.sessions() as s:
            for name,table in Base.metadata.tables.items():
                if name in {'auth_sessions','auth_login_attempts','background_jobs','background_job_events'}:continue
                keys=','.join('t."'+c.name+'"' for c in table.primary_key)
                rows=(await s.execute(text(f'SELECT jsonb_build_array({keys})::text,md5(to_jsonb(t)::text) FROM public."{name}" t'))).all()
                result[name]=dict(rows)
            result['checkpoints']={}
            for name in ['checkpoint_migrations','checkpoints','checkpoint_blobs','checkpoint_writes']:
                row=(await s.execute(text(f"SELECT count(*),md5(COALESCE(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM smart_cs_checkpoints.{name} t"))).one()
                result['checkpoints'][name]=list(row)
        return result
    finally:await db.close()

def main():
    load_dotenv(ENV_PATH);OUT.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'started_at':datetime.now(timezone.utc).isoformat(),'jobs':{}}
    stage='baseline';before=asyncio.run(snapshot())
    baseline=OUT/'live-before.json'
    if baseline.exists():before=json.loads(baseline.read_text(encoding='utf-8'))
    else:baseline.write_text(json.dumps(before,indent=2)+'\n',encoding='utf-8')
    try:
        report['frozen_assets']=validate(ROOT,CORPUS)
        sources=json.loads((CORPUS/'source_manifest.json').read_text(encoding='utf-8'))['sources']
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=30) as client:
            assert client.get('/api/admin/jobs').status_code==401
            with authenticated_client(client,'customer-a'):
                assert client.post('/api/admin/jobs',json={'idempotency_key':'forbidden','work':{'kind':'index'}}).status_code==403
            with authenticated_client(client,'admin') as admin:
                stage='imports'
                def submit(key,work):
                    response=client.post('/api/admin/jobs',json={'idempotency_key':'step19-acceptance-'+key,'work':work});response.raise_for_status();return response.json()
                def wait(job,timeout=1000,kill=False):
                    started=time.monotonic();killed=False;seen=[]
                    while time.monotonic()-started<timeout:
                        row=client.get('/api/admin/jobs/'+job['id']);row.raise_for_status();row=row.json()
                        marker=(row['status'],row['stage'],row['attempt'])
                        if not seen or seen[-1]!=marker:
                            seen.append(marker);print(json.dumps({'job':job['kind'],'state':marker,'progress':row['progress'],'total':row['total']}),flush=True)
                        if kill and not killed and row['status']=='running' and row['stage']=='embedding' and row['progress']>=8:
                            compose('kill','-s','SIGKILL','worker');compose('start','worker');killed=True
                            report['real_worker_killed_mid_embedding']=True
                        if row['status'] in {'succeeded','failed','cancelled'}:
                            assert row['status']=='succeeded',row.get('error_code')
                            row['observed_states']=seen;return row
                        health=client.get('/health');assert health.status_code==200
                        time.sleep(2)
                    raise TimeoutError('Job did not complete within finite acceptance deadline')
                for label,source,publish in [('pdf',sources[0],True),('html',sources[1],False)]:
                    raw=(CORPUS/'sources'/source['file']).read_bytes();assert hashlib.sha256(raw).hexdigest()==source['sha256']
                    upload=client.post('/api/admin/job-artifacts',params={'filename':source['file']},content=raw);upload.raise_for_status()
                    work={'kind':'ingest',**{k:upload.json()[k] for k in ['artifact_sha256','extension']},'publish':publish,
                        'document':{'source_key':'step19-worker-qa:'+label,'title':source['title']+' [private worker acceptance]',
                            'version':'step19-v1','domain':source['collection'],'access_scope':'private','owner_id':admin['id'],
                            'effective_from':source['effective_from'],'source_type':source['source_type'],'publisher':source['publisher'],
                            'license':source['license'],'source_url':source['url'],'expected_sha256':source['sha256']}}
                    job=submit(label,work);assert submit(label,work)['id']==job['id']
                    report['jobs'][label]=wait(job)
                stage='index_restart'
                index_job=submit('index',{'kind':'index'});report['jobs']['index']=wait(index_job,kill=True)
                assert report['jobs']['index']['result']['active'] is False
                assert report['jobs']['index']['attempt']>=2, 'A fresh acceptance must include a real killed worker attempt'
                stage='evaluation'
                report['jobs']['evaluation']=wait(submit('evaluation',{'kind':'retrieval_evaluation','split':'validation'}))
                stage='duplicate_delivery'
                identifier=report['jobs']['pdf']['id']
                # Deliver the same completed job twice through the real Redis broker.
                code="from smart_cs.background.celery_app import execute; execute.delay('"+identifier+"'); execute.delay('"+identifier+"'); print('delivered')"
                compose('exec','-T','worker','python','-c',code);time.sleep(5)
                duplicate=client.get('/api/admin/jobs/'+identifier).json()
                assert duplicate['attempt']==report['jobs']['pdf']['attempt'] and duplicate['result']==report['jobs']['pdf']['result']
                stage='worker_restart_result'
                compose('restart','worker')
                assert client.get('/api/admin/jobs/'+identifier).json()['result']==duplicate['result']
                stage='permissions'
                db=configured_database()
                async def privileges():
                    try:
                        async with db.sessions() as s:
                            return await s.scalar(text("SELECT NOT has_table_privilege('smart_cs_jobs','orders','INSERT,UPDATE,DELETE') AND NOT has_table_privilege('smart_cs_jobs','auth_credentials','SELECT') AND NOT has_table_privilege('smart_cs_jobs','retrieval_indexes','UPDATE')"))
                    finally:await db.close()
                assert asyncio.run(privileges())
                report.update(unauthenticated_denied=True,customer_submission_denied=True,limited_worker_role=True,duplicate_redis_delivery_unchanged=True,worker_restart_result_persisted=True,api_health_while_running=True)
        stage='preservation';after=asyncio.run(snapshot())
        for name,rows in before.items():
            if name=='checkpoints':assert after[name]==rows
            else:
                assert all(after[name].get(key)==value for key,value in rows.items()),name+' original rows changed'
                if name not in {'knowledge_documents','knowledge_chunks','knowledge_ingestions','retrieval_indexes','retrieval_index_documents','knowledge_embeddings'}:assert after[name]==rows,name+' unexpected rows'
        report.update(passed=True,stage='complete',original_rows_preserved=True,checkpoints_preserved=True,
            row_deltas={name:len(after[name])-len(rows) for name,rows in before.items() if name!='checkpoints' and len(after[name])!=len(rows)},
            quality_claim='validation diagnostic only; no human gold accuracy claim')
    except Exception as error:report.update(stage=stage,error_type=type(error).__name__)
    (OUT/'live-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'jobs','frozen_assets'}},ensure_ascii=False,indent=2));return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
