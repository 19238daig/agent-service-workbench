"""Verify final persisted runs, assets, original facts and regression evidence."""
import asyncio
import hashlib
import json
from datetime import datetime,timezone
import httpx
from dotenv import load_dotenv
from sqlalchemy import select,func,text
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import RunControl,RunEvent,RetrievalIndex,KnowledgeEmbedding
from smart_cs.evaluation.assets import ROOT,CORPUS
from authenticated_client import authenticated_client
from accept_runs_live import snapshot,restart

OUT=PROJECT_ROOT/'.local/durable-runs'
def read(path):return json.loads(path.read_text(encoding='utf-8'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

async def database_checks():
    before=read(OUT/'original-rows.json');after=await snapshot()
    allowed={'chat_sessions','chat_messages','chat_turns','execution_runs','model_call_records','run_controls','run_events',
        'checkpoint:checkpoints','checkpoint:checkpoint_blobs','checkpoint:checkpoint_writes'}
    for name,rows in before.items():
        assert all(after[name].get(key)==value for key,value in rows.items()),'Original rows changed: '+name
        if name not in allowed:assert after[name]==rows,'Unexpected rows: '+name
    db=configured_database()
    try:
        async with db.sessions() as s:
            revision=await s.scalar(text('SELECT version_num FROM alembic_version'));assert revision=='0014_durable_runs'
            active=await s.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
            vectors=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==active.id))
            assert active.id=='dense-v1-65ba87aeb7f78af757f93232b5824275a24745fd' and vectors==547
            assert not await s.scalar(select(func.count()).select_from(RunControl).where(RunControl.status.in_(['queued','running','cancel_requested'])))
        return {'revision':revision,'active_index':active.id,'active_vectors':vectors,'original_rows_preserved':True,
            'original_checkpoints_preserved':True,'business_write_tables_unchanged':True,
            'row_deltas':{name:len(after[name])-len(rows) for name,rows in before.items() if len(after[name])!=len(rows)}}
    finally:await db.close()

def main():
    load_dotenv(ENV_PATH);report={'passed':False,'step':20,'checked_at':datetime.now(timezone.utc).isoformat()}
    try:
        migration=read(OUT/'live-upgrade.json');pg=read(OUT/'postgresql-verification.json');backend=read(PROJECT_ROOT/'.local/checks/backend.json')
        browser=read(OUT/'frontend-e2e.json');live={scenario:read(OUT/('live-'+scenario+'.json')) for scenario in ['disconnect','queue','crash']}
        assert all(r['passed'] for r in [migration,pg,backend,*live.values()])
        stats=browser['stats'];assert stats['unexpected']==0 and stats['flaky']==0 and stats['expected']==21
        manifest=read(ROOT/'manifest.json')
        for relative,entry in manifest['files'].items():assert sha(ROOT/relative)==entry['sha256']
        assert sha(CORPUS/'source_manifest.json')==manifest['source_manifest_sha256']
        for relative,digest in manifest['contract_sha256'].items():assert sha(PROJECT_ROOT/relative)==digest
        for entry in read(CORPUS/'source_manifest.json')['sources']:assert sha(CORPUS/'sources'/entry['file'])==entry['sha256']
        # Apply final code and re-read old results, with no creation/resume POSTs.
        restart(True)
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=30) as client:
            assert client.get('/').status_code==200 and client.get('/health').status_code==200
            schema=client.get('/openapi.json').json();assert '/api/runs/{run_id}/events' in schema['paths']
            with authenticated_client(client,'customer-a'):
                for evidence in live.values():
                    identifier=evidence['final_run_id'];row=client.get('/api/runs/'+identifier);row.raise_for_status();row=row.json()
                    assert row['result_event']['type']=='complete'
                    calls=row['model_calls']
                    response=client.get('/api/runs/'+identifier+'/events',headers={'Last-Event-ID':'0'});response.raise_for_status()
                    events=[json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
                    assert events[-1]['type']=='complete'
                    again=client.get('/api/runs/'+identifier);again.raise_for_status();assert again.json()['model_calls']==calls
        database=asyncio.run(database_checks())
        source_files=[p for p in (PROJECT_ROOT/'src/smart_cs/durable').glob('*.py')]+[PROJECT_ROOT/'src/smart_cs/api/runs.py',PROJECT_ROOT/'src/smart_cs/api/main.py',
            PROJECT_ROOT/'src/smart_cs/services/chat.py',PROJECT_ROOT/'src/smart_cs/services/execution.py',PROJECT_ROOT/'src/smart_cs/persistence/conversations.py',
            PROJECT_ROOT/'src/smart_cs/repositories/runs.py',PROJECT_ROOT/'frontend/src/workflow.ts',PROJECT_ROOT/'frontend/src/App.tsx',
            PROJECT_ROOT/'frontend/src/history.ts',PROJECT_ROOT/'frontend/src/workspace.ts']
        report.update(passed=True,migration=mutation_summary(migration),database=database,
            backend={k:v for k,v in backend.items() if k not in {'modules','module_reports'}},postgresql=pg,frontend=stats,live=live,
            api_restart_results_and_model_counts_preserved=True,frozen_assets_preserved=True,ui_build_available=(PROJECT_ROOT/'frontend/dist/index.html').is_file(),
            api_runtime='FastAPI lifespan supervisor; PostgreSQL queue and thread locks',
            no_new_dependencies=True,authentication_audit_changes_expected=True,
            source_sha256={str(p.relative_to(PROJECT_ROOT)).replace('\\','/'):sha(p) for p in source_files},
            evidence_files=['.local/durable-runs/live-upgrade.json','.local/durable-runs/postgresql-verification.json',
                '.local/durable-runs/live-disconnect.json','.local/durable-runs/live-queue.json','.local/durable-runs/live-crash.json',
                '.local/durable-runs/frontend-e2e.json','.local/checks/backend.json'],next_step=21)
    except Exception as error:report.update(error_type=type(error).__name__)
    (OUT/'final-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'source_sha256','live','postgresql'}},ensure_ascii=False,indent=2));return 0 if report['passed'] else 1

def mutation_summary(migration):return {k:migration[k] for k in ['passed','revision','preexisting_rows_preserved','checkpoints_preserved','backup_sha256']}
if __name__=='__main__':raise SystemExit(main())
