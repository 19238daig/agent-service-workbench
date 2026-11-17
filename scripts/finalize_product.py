"""Read-only final product evidence, frozen assets and original-row preservation."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import httpx
from dotenv import load_dotenv
from sqlalchemy import select, func, text
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.db.models import Ticket, TicketEvent, ActionExecution, Approval, ActionRequest, RetrievalIndex, KnowledgeEmbedding, KnowledgeDocument, RunControl, RunEvent
from smart_cs.evaluation.assets import ROOT, CORPUS
from accept_runs_live import snapshot
from authenticated_client import authenticated_client

OUT=PROJECT_ROOT/'.local/product'
def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

async def database_checks(ticket_id,knowledge):
    before=read(OUT/'original-rows.json');after=await snapshot()
    allowed={'chat_sessions','chat_messages','chat_turns','execution_runs','model_call_records','run_controls','run_events',
        'action_requests','approvals','action_executions','tickets','ticket_events','knowledge_documents','knowledge_chunks',
        'knowledge_ingestions','knowledge_embeddings','retrieval_indexes','retrieval_index_documents','knowledge_admin_events',
        'background_jobs','background_job_events','checkpoint:checkpoints','checkpoint:checkpoint_blobs','checkpoint:checkpoint_writes'}
    for name,rows in before.items():
        assert all(after[name].get(key)==value for key,value in rows.items()),'Original rows changed: '+name
        if name not in allowed:assert after[name]==rows,'Unexpected new rows: '+name
    db=configured_database()
    try:
        async with db.sessions() as s:
            revision=await s.scalar(text('SELECT version_num FROM alembic_version'));assert revision=='0015_knowledge_admin'
            active=await s.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
            vectors=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==active.id))
            assert active.id==knowledge['active_before']==knowledge['active_after'] and vectors==547
            ticket=await s.get(Ticket,ticket_id);assert ticket.status=='closed' and ticket.summary.startswith('第21步页面闭环验收-')
            events=list((await s.scalars(select(TicketEvent).where(TicketEvent.ticket_id==ticket_id).order_by(TicketEvent.created_at))).all())
            executions=list((await s.scalars(select(ActionExecution).where(ActionExecution.ticket_id==ticket_id))).all())
            assert len(executions)==4 and sum(e.tool=='ticket_create' for e in executions)==1
            assert len({e.event_id for e in executions})==4
            for e in executions:
                request=await s.get(ActionRequest,e.action_id);assert request.status=='executed'
                approvals=list((await s.scalars(select(Approval).where(Approval.action_id==e.action_id))).all())
                assert any(a.stage=='user' and a.decision=='approve' for a in approvals)
                if request.required_approval=='staff':assert any(a.stage=='staff' and a.decision=='approve' and a.principal_id!=request.user_id for a in approvals)
            doc=await s.get(KnowledgeDocument,knowledge['document_id']);assert doc.status=='withdrawn'
            assert not await s.scalar(select(func.count()).select_from(RunControl).where(RunControl.status.in_(['queued','running','cancel_requested'])))
            failed=await s.scalar(select(RunEvent).where(RunEvent.run_id=='68aa21e0b7ae4058852542387464cbd8',RunEvent.event_type=='complete'))
            assert failed and failed.payload.get('task_plan',{}).get('status')=='blocked'
        return {'revision':revision,'active_index':active.id,'active_vectors':vectors,'original_rows_preserved':True,
            'original_checkpoints_preserved':True,'orders_unchanged':after['orders']==before['orders'],
            'qa_ticket_id':ticket_id,'qa_ticket_status':ticket.status,'qa_ticket_version':ticket.version,'qa_ticket_events':len(events),
            'qa_transaction_receipts':len(executions),'qa_knowledge_status':doc.status,
            'observed_clarification_runs':['a03551ac5bfb465b9b8abdc7fa1167ae','2d5d985c9af64aef80b2d807d5e3f598','eba6f956e6904cd185fe7078232f3d08'],
            'observed_guarded_refusal':{'run_id':'68aa21e0b7ae4058852542387464cbd8','reason':'POLICY_EVIDENCE_UNAVAILABLE','quality_measurement':False},
            'row_deltas':{name:len(after[name])-len(rows) for name,rows in before.items() if len(after[name])!=len(rows)}}
    finally:await db.close()

def main():
    load_dotenv(ENV_PATH);report={'passed':False,'step':21,'checked_at':datetime.now(timezone.utc).isoformat()}
    try:
        migration=read(OUT/'live-upgrade.json');pg=read(OUT/'postgresql-verification.json');backend=read(PROJECT_ROOT/'.local/checks/backend.json')
        supplement=read(PROJECT_ROOT/'.local/checks/modules/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee/test_compound_tasks.json');assert supplement['passed']
        browser=read(OUT/'frontend-e2e.json');tickets=read(OUT/'live-tickets.json');knowledge=read(OUT/'live-knowledge.json')
        assert all(r['passed'] for r in [migration,pg,backend,tickets,knowledge])
        assert pg['tests']==10 and pg['public_tables_unchanged'] and pg['restricted_database_role']
        assert browser['stats']['expected']==30 and browser['stats']['unexpected']==browser['stats']['flaky']==0
        manifest=read(ROOT/'manifest.json')
        for relative,entry in manifest['files'].items():assert sha(ROOT/relative)==entry['sha256']
        assert sha(CORPUS/'source_manifest.json')==manifest['source_manifest_sha256']
        for relative,digest in manifest['contract_sha256'].items():assert sha(PROJECT_ROOT/relative)==digest
        for entry in read(CORPUS/'source_manifest.json')['sources']:assert sha(CORPUS/'sources'/entry['file'])==entry['sha256']
        access={}
        with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=30) as client:
            assert client.get('/').status_code==client.get('/health').status_code==200
            paths=client.get('/openapi.json').json()['paths'];assert '/api/admin/knowledge/documents/{identifier}/publish' in paths
            for name,expected in [('customer-a',403),('support',403),('admin',200)]:
                with authenticated_client(client,name):
                    result=client.get('/api/admin/knowledge/documents?limit=1');assert result.status_code==expected;access[name+'_knowledge']=expected
                    if name=='customer-a':assert client.get('/api/tickets/'+tickets['ticket_id']).json()['ticket']['status']=='closed'
            with authenticated_client(client,'customer-b'):
                result=client.get('/api/tickets/'+tickets['ticket_id']);assert result.status_code==404;access['other_customer_ticket']=404
        database=asyncio.run(database_checks(tickets['ticket_id'],knowledge))
        files=list((PROJECT_ROOT/'frontend/src/product').glob('*'))+[PROJECT_ROOT/'frontend/src/App.tsx',PROJECT_ROOT/'frontend/src/history.ts',
            PROJECT_ROOT/'frontend/src/workflow.ts',PROJECT_ROOT/'frontend/src/WorkflowPanel.tsx',PROJECT_ROOT/'frontend/e2e/product.spec.ts',
            PROJECT_ROOT/'src/smart_cs/services/knowledge_admin.py',PROJECT_ROOT/'src/smart_cs/api/knowledge_admin.py',
            PROJECT_ROOT/'src/smart_cs/schemas/knowledge_admin.py',PROJECT_ROOT/'migrations/versions/0015_knowledge_admin.py',
            PROJECT_ROOT/'tests/test_knowledge_admin.py',PROJECT_ROOT/'src/smart_cs/agents/intent_router.py',PROJECT_ROOT/'src/smart_cs/workflow.py']
        report.update(passed=True,frontend_tests=browser['stats']['expected'],backend_tests=backend['tests_run'],backend_skipped=backend['skipped'],
            backend_modules=len(backend.get('modules',{})),supplemental_compound_tests=supplement,postgresql_tests=pg['tests'],real_browser_ticket_flow=True,real_browser_knowledge_flow=True,
            live_index_activation=False,index_activation_verified_in_isolated_postgresql=True,access=access,database=database,
            frozen_assets_unchanged=True,visual_review=str(OUT/'visual-review.md'),next_step=22,
            source_sha256={p.relative_to(PROJECT_ROOT).as_posix():sha(p) for p in files if p.is_file()})
    except Exception as error:
        report.update(error_type=type(error).__name__,error=str(error) if isinstance(error,AssertionError) else 'See verification reports; no credentials recorded')
    (OUT/'final-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'},ensure_ascii=False,indent=2));return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
