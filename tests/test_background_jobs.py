"""Durable job contracts on SQLite; session fencing/recovery on real PostgreSQL."""
import asyncio
import os
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from pydantic import ValidationError
from sqlalchemy import select,update
from smart_cs.background.service import JobService,dispatch_once,now
from smart_cs.background.runtime import Worker
from smart_cs.background.files import JobFiles
from smart_cs.db.models import BackgroundJob as Job,AuthCredential,User
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.jobs import JobRequest
from test_action_plans import ActionPlanFixture

class BackgroundJobsTests(ActionPlanFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.db.sessions.begin() as s:
            s.add(AuthCredential(user_id=self.admin.subject_id,login_name=self.admin.subject_id,password_hash='not-a-login-fixture',enabled=True))
        self.jobs=JobService(self.db,Path(self.temp.name))
    async def new(self,**changes):
        return await self.jobs.submit(self.admin,JobRequest.model_validate({'idempotency_key':uuid4().hex,'work':{'kind':'index'},**changes}))
    def postgres(self):
        if self.db.engine.dialect.name!='postgresql':self.skipTest('Session fencing requires the isolated PostgreSQL harness')
    async def test_strict_job_contract(self):
        for work in [{'kind':'shell','command':'whoami'},{'kind':'index','activate':True},{'kind':'retrieval_evaluation','split':'test'},{'kind':'ingest','path':'C:/secret'}]:
            with self.assertRaises(ValidationError):JobRequest.model_validate({'idempotency_key':'x','work':work})
    async def test_admin_required(self):
        with self.assertRaises(ApplicationError):await self.jobs.submit(self.a,{'idempotency_key':'x','work':{'kind':'index'}})
        async with self.db.sessions.begin() as s:await s.execute(update(AuthCredential).where(AuthCredential.user_id==self.admin.subject_id).values(enabled=False))
        with self.assertRaises(ApplicationError):await self.new()
    async def test_idempotency_and_parameter_conflict(self):
        key=uuid4().hex;a=await self.new(idempotency_key=key);b=await self.new(idempotency_key=key)
        self.assertEqual(a['id'],b['id'])
        with self.assertRaises(ApplicationError):await self.new(idempotency_key=key,work={'kind':'retrieval_evaluation'})
    async def test_other_admin_cannot_read_job(self):
        job=await self.new()
        with self.assertRaises(ApplicationError):await self.jobs.get(self.b,job['id'])
    async def test_queued_cancel_and_event_order(self):
        job=await self.new();result=await self.jobs.cancel(self.admin,job['id']);self.assertEqual(result['status'],'cancelled')
        self.assertEqual([e['version'] for e in await self.jobs.events(self.admin,job['id'])],[1,2])
    async def test_broker_failure_keeps_outbox(self):
        job=await self.new()
        async def unavailable(_):raise ConnectionError('secret broker URL must not appear')
        await dispatch_once(self.db,unavailable)
        current=await self.jobs.get(self.admin,job['id']);self.assertEqual(current['status'],'queued');self.assertIsNone(current['error_code'])
        async with self.db.sessions.begin() as s:await s.execute(update(Job).where(Job.id==job['id']).values(next_dispatch_at=now()-timedelta(seconds=1)))
        delivered=[]
        async def publish(identifier):delivered.append(identifier)
        await dispatch_once(self.db,publish);self.assertIn(job['id'],delivered)
    async def test_artifact_integrity_and_paths(self):
        files=JobFiles(Path(self.temp.name));data=files.store(b'<html>Real fixture</html>','.html')
        self.assertEqual(files.verify(data['artifact_sha256'],'.html').read_bytes(),b'<html>Real fixture</html>')
        files.input(data['artifact_sha256'],'.html').write_bytes(b'changed')
        with self.assertRaises(ApplicationError):files.verify(data['artifact_sha256'],'.html')
        with self.assertRaises(ApplicationError):files.input('../escape','.html')
    async def test_real_worker_duplicate_delivery_once(self):
        self.postgres();job=await self.new();entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def handler(ctx,db):
            calls.append(ctx.job.id);entered.set();await release.wait();await ctx.progress('tested',1,1);return {'ok':True}
        running=asyncio.create_task(Worker(self.db,handler).run(job['id']));await asyncio.wait_for(entered.wait(),10)
        try:self.assertEqual((await Worker(self.db,handler).run(job['id']))['status'],'duplicate_running')
        finally:release.set()
        self.assertEqual((await running)['status'],'succeeded')
        await Worker(self.db,handler).run(job['id']);self.assertEqual(len(calls),1)
    async def test_worker_process_loss_reclaims_attempt(self):
        self.postgres();job=await self.new()
        async with self.db.sessions.begin() as s:await s.execute(update(Job).where(Job.id==job['id']).values(status='running',attempt=1,attempt_token=uuid4().hex,lease_until=now()-timedelta(seconds=60)))
        async def handler(ctx,db):return {'recovered':True}
        self.assertEqual((await Worker(self.db,handler).run(job['id']))['status'],'succeeded')
        self.assertEqual((await self.jobs.get(self.admin,job['id']))['attempt'],2)
    async def test_three_attempt_budget(self):
        self.postgres();job=await self.new()
        async def failure(ctx,db):raise TimeoutError('provider secret')
        for attempt in range(3):
            await Worker(self.db,failure).run(job['id'])
            async with self.db.sessions.begin() as s:await s.execute(update(Job).where(Job.id==job['id']).values(updated_at=now()-timedelta(minutes=1),next_dispatch_at=now()-timedelta(minutes=1)))
        result=await self.jobs.get(self.admin,job['id']);self.assertEqual((result['status'],result['attempt'],result['error_code']),('failed',3,'JOB_EXECUTION_FAILED'))
        with self.assertRaises(ApplicationError):await self.jobs.retry(self.admin,job['id'],result['payload_hash'])
    async def test_revocation_before_worker_claim(self):
        self.postgres();job=await self.new()
        async with self.db.sessions.begin() as s:await s.execute(update(User).where(User.id==self.admin.subject_id).values(role='customer'))
        calls=[]
        async def handler(ctx,db):calls.append(True)
        self.assertEqual((await Worker(self.db,handler).run(job['id']))['status'],'failed');self.assertFalse(calls)
    async def test_cancellation_fences_business_commit(self):
        self.postgres();job=await self.new()
        async def handler(ctx,db):
            async with db.sessions.begin() as s:
                user=await s.get(User,self.a.subject_id);user.display_name='MUST ROLLBACK'
                await self.jobs.cancel(self.admin,job['id'])
            return {'should_not_happen':True}
        self.assertEqual((await Worker(self.db,handler).run(job['id']))['status'],'cancelled')
        async with self.db.sessions() as s:self.assertEqual((await s.get(User,self.a.subject_id)).display_name,'Fixture')
    async def test_revocation_fences_business_commit(self):
        self.postgres();job=await self.new()
        async def handler(ctx,db):
            async with db.sessions.begin() as s:
                user=await s.get(User,self.a.subject_id);user.display_name='MUST ROLLBACK'
                async with self.db.sessions.begin() as control:await control.execute(update(AuthCredential).where(AuthCredential.user_id==self.admin.subject_id).values(enabled=False))
            return {}
        await Worker(self.db,handler).run(job['id'])
        async with self.db.sessions() as s:
            self.assertEqual((await s.get(User,self.a.subject_id)).display_name,'Fixture')
            self.assertEqual((await s.get(Job,job['id'])).error_code,'JOB_ADMIN_REVOKED')
    async def test_commit_then_redelivery_reuses_ingestion_receipt(self):
        self.postgres();job=await self.new();calls=[];path=Path(self.temp.name)/'receipt.txt';path.write_text('A genuine test fixture. '*30,encoding='utf-8')
        from smart_cs.ingestion.pipeline import IngestionPipeline
        spec={'source_key':uuid4().hex,'title':'Fixture','version':'1','domain':'after_sales','access_scope':'private','owner_id':self.admin.subject_id,'effective_from':'2020-01-01','source_type':'fixture','publisher':'Fixture','license':'test'}
        async def handler(ctx,db):
            result=await IngestionPipeline(db,root=Path(self.temp.name)).ingest(path,spec,publish=True)
            calls.append(result)
            if len(calls)==1:raise TimeoutError('lost after document commit')
            return result
        await Worker(self.db,handler).run(job['id'])
        async with self.db.sessions.begin() as s:await s.execute(update(Job).where(Job.id==job['id']).values(updated_at=now()-timedelta(minutes=1)))
        self.assertEqual((await Worker(self.db,handler).run(job['id']))['status'],'succeeded')
        self.assertEqual(calls[0]['document_id'],calls[1]['document_id']);self.assertTrue(calls[1]['unchanged'])

    async def test_lost_lock_connection_cannot_commit(self):
        self.postgres();job=await self.new()
        from sqlalchemy import text
        async def handler(ctx,db):
            async with db.sessions() as s:pid=await s.scalar(text('SELECT pg_backend_pid()'))
            async with self.db.sessions.begin() as control:await control.execute(text('SELECT pg_terminate_backend(:pid)'),{'pid':pid})
            async with db.sessions.begin() as s:
                user=await s.get(User,self.a.subject_id);user.display_name='MUST ROLLBACK'
            return {}
        result=await Worker(self.db,handler).run(job['id']);self.assertEqual(result['status'],'retry_wait')
        async with self.db.sessions() as s:self.assertEqual((await s.get(User,self.a.subject_id)).display_name,'Fixture')

    async def test_evaluation_resume_skips_completed_cases(self):
        self.postgres();job=await self.new()
        import hashlib,json
        from types import SimpleNamespace
        from smart_cs.background.handlers import evaluate
        from smart_cs.evaluation.assets import records,ROOT as ASSETS
        from smart_cs.evaluation.schema import RagCase
        cases=[r for r in records(ASSETS) if isinstance(r,RagCase) and r.split=='validation' and r.evaluation_scope=='retrieval'][:2]
        root=Path(self.temp.name)/uuid4().hex;root.mkdir();(root/'manifest.json').write_text(json.dumps({'source_manifest_sha256':'frozen'}))
        binding={'index_id':'fixture-index','dataset_sha256':hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()}
        async with self.db.sessions.begin() as s:await s.execute(update(Job).where(Job.id==job['id']).values(binding=binding))
        calls=[];once=[False]
        class Retriever:
            def __init__(self,db):pass
            async def active(self,s):return SimpleNamespace(id='fixture-index',configuration={'source_manifest_hash':'frozen'})
            async def search(self,query,top_k):
                calls.append(query)
                if query==cases[1].message and not once[0]:once[0]=True;raise TimeoutError('interrupted evaluation')
                return {'index_id':'fixture-index','documents':[]}
        async def handler(ctx,db):return await evaluate(ctx,'validation')
        with patch('smart_cs.evaluation.assets.ROOT',root),patch('smart_cs.evaluation.assets.validate',lambda *a:None),patch('smart_cs.evaluation.assets.records',lambda *a:cases),patch('smart_cs.retrieval.dense.DenseRetriever',Retriever):
            await Worker(self.db,handler).run(job['id'])
            async with self.db.sessions.begin() as s:await s.execute(update(Job).where(Job.id==job['id']).values(updated_at=now()-timedelta(minutes=1)))
            self.assertEqual((await Worker(self.db,handler).run(job['id']))['status'],'succeeded')
        self.assertEqual(calls.count(cases[0].message),1);self.assertEqual(calls.count(cases[1].message),2)

    async def test_cancel_keeps_already_committed_receipt(self):
        self.postgres();job=await self.new()
        async def handler(ctx,db):
            await ctx.receipt({'document_id':'already-committed','active_index_changed':False})
            await self.jobs.cancel(self.admin,job['id'])
            await ctx.progress('after_commit',1,1)
        await Worker(self.db,handler).run(job['id'])
        result=await self.jobs.get(self.admin,job['id']);self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['result']['document_id'],'already-committed')

    async def test_http_auth_and_queue_contract(self):
        import httpx
        from fastapi import FastAPI
        from smart_cs.api.jobs import router
        from smart_cs.api.dependencies import current_identity
        from smart_cs.api.observability import install_error_handlers
        app=FastAPI();app.state.database=self.db;app.include_router(router);install_error_handlers(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            self.assertEqual((await client.get('/api/admin/jobs')).status_code,401)
            app.dependency_overrides[current_identity]=lambda:self.a
            self.assertEqual((await client.post('/api/admin/jobs',json={'idempotency_key':'denied','work':{'kind':'index'}})).status_code,403)
            app.dependency_overrides[current_identity]=lambda:self.admin
            response=await client.post('/api/admin/jobs',json={'idempotency_key':'accepted','work':{'kind':'index'}})
            self.assertEqual(response.status_code,202);job=response.json();self.assertEqual(job['status'],'queued')
            self.assertEqual((await client.get('/api/admin/jobs/'+job['id'])).json()['id'],job['id'])
            self.assertEqual((await client.post('/api/admin/jobs/'+job['id']+'/cancel')).json()['status'],'cancelled')
