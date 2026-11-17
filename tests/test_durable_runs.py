"""Commands, checkpoints, public SSE replay and cancellation independent of HTTP."""
import asyncio
import json
import unittest
from contextlib import suppress
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from sqlalchemy import select,func,update
from langgraph.graph import StateGraph,END
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import AIMessage
from test_action_plans import ActionPlanFixture
from smart_cs.agents.supervisor import AgentState
from smart_cs.db.database import Database
from smart_cs.db.models import AuthCredential,RunControl,RunEvent,ChatMessage,ChatTurn,ExecutionRun,ModelCallRecord,utcnow
from smart_cs.durable.service import RunService
from smart_cs.durable.executor import RunExecutor
from smart_cs.durable.store import public_event
from smart_cs.persistence.conversations import ConversationRuntime
from smart_cs.persistence.postgres import MemoryCheckpointLocks,PostgresCheckpointStore,prepare_checkpoint_store
from smart_cs.persistence.context import GRAPH_VERSION,execution_binding
from smart_cs.services.chat import ChatService
from smart_cs.repositories.runs import DatabaseRunRepository
from smart_cs.schemas.chat import ChatRequest
from smart_cs.schemas.errors import ApplicationError
from smart_cs.observability.models import invoke_model
from smart_cs.tracing.otel_config import AgentMetrics
from smart_cs.api.dependencies import current_identity
from smart_cs.api.observability import install_error_handlers
from smart_cs.api.runs import router

class DurableRunsTests(ActionPlanFixture,unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.schema:
            async def prepare():
                db=Database(cls.url,schema=cls.schema)
                try:await prepare_checkpoint_store(db)
                finally:await db.close()
            asyncio.run(prepare())
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.db.sessions.begin() as s:
            s.add_all([AuthCredential(user_id=i.subject_id,login_name=i.subject_id,password_hash='fixture-not-a-password',enabled=True) for i in [self.a,self.b,self.admin]])
        if self.schema:self.store=await PostgresCheckpointStore(self.db).open()
        else:self.store=MemoryCheckpointLocks();self.store.saver=InMemorySaver()
        self.calls={};self.hold=None;self.entered=asyncio.Event();self.release=asyncio.Event()
        self.repo=DatabaseRunRepository(self.db)
        graph=StateGraph(AgentState);fixture=self
        class Model:
            def __init__(self,name):self.name=name;self.model_name='fixture-'+name
            async def ainvoke(self,*args):
                fixture.calls[self.name]=fixture.calls.get(self.name,0)+1
                if fixture.hold==self.name:fixture.entered.set();await fixture.release.wait()
                return AIMessage(content='reviewed answer',usage_metadata={'input_tokens':5,'output_tokens':2,'total_tokens':7})
        async def intent(state):
            await invoke_model(Model('intent_router'),'fixture')
            return {**(execution_binding.get() or {}),'intent':'clarification','sub_results':{'draft':'PRIVATE_DRAFT_SENTINEL'}}
        async def compliance(state):
            await invoke_model(Model('compliance_check'),'fixture')
            return {'compliance_passed':True}
        async def synthesize(state):return {'final_response':'reviewed answer','messages':[AIMessage(content='reviewed answer')]}
        for name,node in [('intent_router',intent),('compliance_check',compliance),('synthesize',synthesize)]:graph.add_node(name,node)
        graph.set_entry_point('intent_router');graph.add_edge('intent_router','compliance_check');graph.add_edge('compliance_check','synthesize');graph.add_edge('synthesize',END)
        self.graph=graph.compile(checkpointer=self.store.saver).with_config(metadata={'graph_version':GRAPH_VERSION,'agent_writes_enabled':False,'workflow_models':{'intent_router':'fixture'}})
        self.chat=ChatService(self.graph,None,self.repo,AgentMetrics(),ConversationRuntime(self.db,self.store,self.graph,self.repo))
        self.runs=RunService(self.db,self.chat);self.executor=RunExecutor(self.db,lambda:self.chat)
    async def asyncTearDown(self):
        await self.executor.close();await self.store.close();await super().asyncTearDown()
    async def submit(self,**kwargs):
        request=ChatRequest(message='question',client_request_id=kwargs.pop('key',uuid4().hex),**kwargs)
        return await self.runs.submit(request,self.a)
    async def state(self,identifier):
        async with self.db.sessions() as s:return await s.get(RunControl,identifier)
    async def running(self,job):
        task=asyncio.create_task(self.executor.execute(job['run_id']))
        await asyncio.wait_for(self.entered.wait(),10)
        return task

    async def test_submit_is_durable_and_idempotent_without_model_calls(self):
        key=uuid4().hex;a=await self.submit(key=key);b=await self.submit(key=key)
        self.assertEqual(a['run_id'],b['run_id']);self.assertFalse(self.calls)
        self.assertEqual(a['status'],'queued')
        with self.assertRaisesRegex(ApplicationError,'RUN_IDEMPOTENCY_CONFLICT'):
            await self.runs.submit(ChatRequest(message='changed',client_request_id=key),self.a)
        async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id==a['session_id'])),1)

    async def test_terminal_event_and_reviewed_history_commit_together(self):
        job=await self.submit();await self.executor.execute(job['run_id'])
        detail=await self.runs.detail(job['run_id'],self.a);self.assertEqual(detail['status'],'succeeded',detail)
        state,events=await self.runs.page(job['run_id'],self.a,0)
        self.assertEqual([e['id'] for e in events],list(range(1,len(events)+1)))
        self.assertEqual(events[-1]['data']['response'],'reviewed answer')
        history=await self.chat.conversations.history(job['session_id'],self.a)
        self.assertEqual(len(history['messages']),2);self.assertNotIn('PRIVATE_DRAFT_SENTINEL',json.dumps(events))
        record=await self.repo.get(job['run_id'],self.a.subject_id)
        self.assertEqual(len(record['model_calls']),2);self.assertEqual(record['usage']['observed_started_calls'],2)

    async def test_disconnect_and_reconnect_do_not_reexecute(self):
        self.hold='compliance_check';job=await self.submit();task=await self.running(job)
        stream=self.runs.subscribe(job['run_id'],self.a,0);frame=await anext(stream)
        self.assertIn('id: 1',frame);await stream.aclose()
        self.assertEqual((await self.state(job['run_id'])).status,'running')
        self.release.set();await task;before=dict(self.calls)
        replay=[f async for f in self.runs.subscribe(job['run_id'],self.a,1)]
        self.assertIn('event: complete',''.join(replay));self.assertEqual(self.calls,before)
        self.assertNotIn('id: 1\n',''.join(replay))

    async def test_duplicate_executors_only_one_graph(self):
        self.hold='intent_router';job=await self.submit();task=await self.running(job)
        other=RunExecutor(self.db,lambda:self.chat)
        await other.execute(job['run_id']);self.release.set();await task
        await other.execute(job['run_id']);self.assertEqual(self.calls,{'intent_router':1,'compliance_check':1})

    async def test_cancel_queued_never_invokes_model(self):
        job=await self.submit();result=await self.runs.cancel(job['run_id'],self.a)
        self.assertEqual(result['status'],'cancelled');await self.executor.execute(job['run_id']);self.assertFalse(self.calls)
        detail=await self.runs.detail(job['run_id'],self.a);self.assertEqual(detail['result_event']['code'],'RUN_CANCELLED')

    async def test_explicit_cancel_stops_following_nodes(self):
        self.hold='intent_router';job=await self.submit();task=await self.running(job)
        await self.runs.cancel(job['run_id'],self.a);await asyncio.wait_for(task,10)
        self.assertEqual(self.calls,{'intent_router':1});self.assertEqual((await self.state(job['run_id'])).status,'cancelled')
        history=await self.chat.conversations.history(job['session_id'],self.a);self.assertEqual(len(history['messages']),1)

    async def test_active_run_cannot_be_resumed(self):
        job=await self.submit()
        with self.assertRaisesRegex(ApplicationError,'RUN_STILL_ACTIVE'):
            await self.runs.submit(ChatRequest(message='resume',session_id=job['session_id']),self.a,resume=True,client_request_id=uuid4().hex)

    async def test_abandoned_run_requires_explicit_resume(self):
        job=await self.submit()
        async with self.db.sessions.begin() as s:
            await s.execute(update(RunControl).where(RunControl.run_id==job['run_id']).values(status='running',attempt_token=uuid4().hex,lease_until=utcnow()-timedelta(minutes=1)))
        await self.executor.execute(job['run_id']);self.assertFalse(self.calls)
        self.assertEqual((await self.state(job['run_id'])).status,'interrupted')
        await self.executor.execute(job['run_id']);self.assertFalse(self.calls)
        resumed=await self.runs.submit(ChatRequest(message='resume',session_id=job['session_id']),self.a,resume=True,client_request_id=uuid4().hex)
        await self.executor.execute(resumed['run_id']);self.assertEqual((await self.state(resumed['run_id'])).status,'succeeded')
        self.assertEqual(self.calls,{'intent_router':1,'compliance_check':1})

    async def test_cancellation_and_explicit_resume_use_checkpoint(self):
        self.hold='compliance_check';job=await self.submit();task=await self.running(job)
        await self.runs.cancel(job['run_id'],self.a);await task;self.release.set();self.hold=None
        resumed=await self.runs.submit(ChatRequest(message='resume',session_id=job['session_id']),self.a,resume=True,client_request_id=uuid4().hex)
        await self.executor.execute(resumed['run_id'])
        self.assertEqual((await self.state(resumed['run_id'])).status,'succeeded')
        self.assertEqual(self.calls['intent_router'],1);self.assertEqual(self.calls['compliance_check'],2)

    async def test_revoked_account_is_not_executed(self):
        job=await self.submit()
        async with self.db.sessions.begin() as s:await s.execute(update(AuthCredential).where(AuthCredential.user_id==self.a.subject_id).values(enabled=False))
        await self.executor.execute(job['run_id']);self.assertFalse(self.calls)
        self.assertEqual((await self.state(job['run_id'])).status,'failed')

    async def test_foreign_reader_and_cursor_validation(self):
        job=await self.submit();await self.executor.execute(job['run_id'])
        for call in [self.runs.detail(job['run_id'],self.b),self.runs.page(job['run_id'],self.b,0),self.runs.cancel(job['run_id'],self.b)]:
            with self.assertRaisesRegex(ApplicationError,'RUN_NOT_FOUND'):await call
        with self.assertRaisesRegex(ApplicationError,'RUN_CURSOR_AHEAD'):await self.runs.page(job['run_id'],self.a,99999)

    async def test_atomic_publication_failure_recovers_completed_checkpoint(self):
        job=await self.submit();original=public_event;failed=[False]
        def fail_once(payload):
            if payload['type']=='complete' and not failed[0]:failed[0]=True;raise RuntimeError('transaction failure before publication')
            return original(payload)
        with patch('smart_cs.durable.store.public_event',fail_once):await self.executor.execute(job['run_id'])
        self.assertEqual((await self.state(job['run_id'])).status,'failed')
        async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id==job['session_id'])),1)
        before=dict(self.calls)
        resumed=await self.runs.submit(ChatRequest(message='resume',session_id=job['session_id']),self.a,resume=True,client_request_id=uuid4().hex)
        await self.executor.execute(resumed['run_id']);self.assertEqual(self.calls,before)
        self.assertEqual((await self.state(resumed['run_id'])).status,'succeeded')

    async def test_http_last_event_id_and_owner_boundary(self):
        job=await self.submit();await self.executor.execute(job['run_id'])
        app=FastAPI();app.include_router(router);install_error_handlers(app);app.state.run_service_factory=lambda:self.runs
        app.dependency_overrides[current_identity]=lambda:self.a
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            path='/api/runs/'+job['run_id']+'/events'
            response=await client.get(path,headers={'Last-Event-ID':'1'})
            self.assertEqual(response.status_code,200);self.assertIn('event: complete',response.text);self.assertNotIn('id: 1\n',response.text)
            self.assertEqual((await client.get(path,headers={'Last-Event-ID':'-1'})).status_code,400)
            self.assertEqual((await client.get(path+'?after=2',headers={'Last-Event-ID':'1'})).status_code,400)
            lookup=await client.get('/api/runs/by-request/'+(await self.state(job['run_id'])).client_request_id)
            self.assertEqual(lookup.json()['run_id'],job['run_id'])
            app.dependency_overrides[current_identity]=lambda:self.b
            self.assertEqual((await client.get(path)).status_code,404)

    async def test_event_projection_does_not_store_raw_graph_objects(self):
        with self.assertRaises(ValueError):public_event({'type':'on_chat_model_stream','raw':'PRIVATE'})
        value=public_event({'type':'node_start','node':'intent_router','raw':'PRIVATE'})
        self.assertNotIn('raw',value)

    async def test_runtime_limit_stops_following_nodes(self):
        self.hold='intent_router';self.executor.max_seconds=1
        job=await self.submit();task=await self.running(job)
        await asyncio.wait_for(task,10)
        detail=await self.runs.detail(job['run_id'],self.a)
        self.assertEqual(detail['result_event']['code'],'RUN_TIME_LIMIT');self.assertEqual(self.calls,{'intent_router':1})


from test_approvals import ApprovalFixture

class DurableExecutionTests(ApprovalFixture,unittest.IsolatedAsyncioTestCase):
    async def setup_run(self):
        from smart_cs.db.models import ChatSession
        from smart_cs.observability.context import RequestCorrelation
        from smart_cs.services.chat import PreparedRun
        state=await self.draft();view=await self.approvals.create(self.a,state)
        await self.approvals.decide(self.a,view['action_id'],self.decision(view))
        token=uuid4().hex
        async with self.db.sessions.begin() as s:
            s.add(AuthCredential(user_id=self.a.subject_id,login_name=self.a.subject_id,password_hash='fixture',enabled=True))
            run=await s.get(ExecutionRun,state['run_id']);session=await s.get(ChatSession,state['session_id']);turn=await s.get(ChatTurn,state['turn_id'])
            session.active_run_id=run.id;session.next_sequence=2
            p=PreparedRun(self.a,session.id,RequestCorrelation(run.request_id,run.trace_id,run.id),run.id,persistent=True,
                thread_id=session.thread_id,turn_id=turn.id,user_message_id=turn.user_message_id,durable=True,attempt_token=token)
            self.runtime=ConversationRuntime(self.db,None,None,DatabaseRunRepository(self.db));p.record=self.runtime.record(p);run.snapshot=p.record
            s.add(RunControl(run_id=run.id,user_id=self.a.subject_id,session_id=session.id,turn_id=turn.id,client_request_id=uuid4().hex,
                request_hash='a'*64,kind='new',status='running',attempt_token=token,next_event_sequence=1))
        return p,view
    async def execute(self,view):
        from smart_cs.services.execution import ActionExecutionService
        return await ActionExecutionService(self.db,clock=lambda:self.now).execute(self.a,view['action_id'],view['parameter_hash'])
    async def test_cancelled_run_cannot_commit_approved_action(self):
        from smart_cs.durable.context import run_attempt
        from smart_cs.db.models import ActionExecution
        p,view=await self.setup_run()
        async with self.db.sessions.begin() as s:await s.execute(update(RunControl).where(RunControl.run_id==p.run_id).values(status='cancel_requested'))
        binding=run_attempt.set({'run_id':p.run_id,'token':p.attempt_token})
        try:
            with self.assertRaisesRegex(ApplicationError,'RUN_CANCELLED'):await self.execute(view)
        finally:run_attempt.reset(binding)
        async with self.db.sessions() as s:self.assertIsNone(await s.get(ActionExecution,view['action_id']))
    async def test_stale_executor_token_cannot_commit_action(self):
        from smart_cs.durable.context import run_attempt
        p,view=await self.setup_run();binding=run_attempt.set({'run_id':p.run_id,'token':uuid4().hex})
        try:
            with self.assertRaisesRegex(ApplicationError,'RUN_CANCELLED'):await self.execute(view)
        finally:run_attempt.reset(binding)
    async def test_cancel_after_commit_preserves_receipt(self):
        from smart_cs.durable.context import run_attempt
        p,view=await self.setup_run();binding=run_attempt.set({'run_id':p.run_id,'token':p.attempt_token})
        runs=RunService(self.db,SimpleNamespace(conversations=None))
        try:
            receipt=await self.execute(view)
            await runs.cancel(p.run_id,self.a)
            await self.runtime.finish(p,{**p.record,'status':'cancelled','error_code':'RUN_CANCELLED'},[],None)
            result=await runs.detail(p.run_id,self.a)
            self.assertEqual(result['status'],'cancelled');self.assertEqual(result['result_event']['action_execution'],receipt)
        finally:run_attempt.reset(binding)
    async def test_two_guarded_executors_commit_one_receipt(self):
        if not self.schema:self.skipTest('Requires PostgreSQL transaction row locking')
        from smart_cs.durable.context import run_attempt
        from smart_cs.db.models import ActionExecution
        p,view=await self.setup_run();binding=run_attempt.set({'run_id':p.run_id,'token':p.attempt_token})
        try:a,b=await asyncio.gather(self.execute(view),self.execute(view))
        finally:run_attempt.reset(binding)
        self.assertEqual(a,b)
        async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(ActionExecution).where(ActionExecution.action_id==view['action_id'])),1)
