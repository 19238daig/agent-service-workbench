"""Actual PostgreSQL saver, scoped threads, crash windows and canonical publication.

Run via scripts/verify_conversations.py (restricted role, disposable schema).
No model/network calls, Redis dependency, public table writes or arbitrary replay.
"""
import asyncio
import os
import unittest
from contextlib import AsyncExitStack
from uuid import uuid4
from sqlalchemy import select,update
from langgraph.graph import StateGraph,END
from langchain_core.messages import AIMessage,HumanMessage
from smart_cs.agents.supervisor import AgentState,create_supervisor_graph
from smart_cs.db.database import Database
from smart_cs.db.models import User,ChatSession,ChatMessage,ChatTurn,ExecutionRun,KnowledgeDocument
from smart_cs.persistence.context import GRAPH_VERSION,active_saver,execution_binding
from smart_cs.persistence.postgres import PostgresCheckpointStore,prepare_checkpoint_store,serializer
from smart_cs.persistence.conversations import ConversationRuntime
from smart_cs.repositories.runs import DatabaseRunRepository
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.chat import ChatRequest,ResumeRequest
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.chat import ChatService
from smart_cs.tracing.otel_config import AgentMetrics


class ForbiddenMemory:
    async def add_message(self,*args):raise AssertionError('Redis must not be the product authority')


@unittest.skipUnless(os.getenv('AUTH_VERIFY_SCHEMA'),'Requires restricted PostgreSQL verification runner')
class ConversationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.url=os.environ['DATABASE_URL'];cls.schema=os.environ['AUTH_VERIFY_SCHEMA']
        async def seed():
            db=Database(cls.url,schema=cls.schema)
            try:
                await db.initialize(create_schema=True)
                await prepare_checkpoint_store(db)
                async with db.sessions.begin() as s:
                    s.add_all([User(id='alice',display_name='alice'),User(id='bob',display_name='bob')])
            finally:await db.close()
        asyncio.run(seed())

    async def asyncSetUp(self):
        self.db=Database(self.url,schema=self.schema)
        self.store=await PostgresCheckpointStore(self.db).open()
        self.repo=DatabaseRunRepository(self.db)
        self.alice=IdentityContext(subject_id='alice',authenticated=True,mode='authenticated_session',role='customer')
        self.bob=self.alice.model_copy(update={'subject_id':'bob'})
        self.calls={};self.fail=False;self.block=False
        self.workflow=self.graph();self.service=self.build(self.workflow)

    async def asyncTearDown(self):
        await self.store.close();await self.db.close()

    def graph(self,writes=False,models=None):
        graph=StateGraph(AgentState)
        def node(name):
            async def process(state):
                self.calls[name]=self.calls.get(name,0)+1
                if name=='compliance_check' and self.fail:raise ApplicationError('MODEL_TIMEOUT','test interruption',504)
                binding=execution_binding.get() or {}
                if name=='intent_router':return {**binding,'intent':'clarification','sub_results':{'draft':'PRIVATE_DRAFT'},'agent_steps':[name]}
                if name=='synthesize':return {**binding,'final_response':'safe blocked' if self.block else 'reviewed answer',
                    'compliance_passed':not self.block,'messages':[AIMessage(content='reviewed answer')]}
                return {**binding,'compliance_passed':not self.block}
            return process
        for name in ['intent_router','compliance_check','synthesize']:graph.add_node(name,node(name))
        graph.set_entry_point('intent_router');graph.add_edge('intent_router','compliance_check')
        graph.add_edge('compliance_check','synthesize');graph.add_edge('synthesize',END)
        return graph.compile(checkpointer=self.store.saver).with_config(metadata={'graph_version':GRAPH_VERSION,'agent_writes_enabled':writes,'workflow_models':models or {'intent_router':'fixture'}})

    def build(self,graph):
        runtime=ConversationRuntime(self.db,self.store,graph,self.repo)
        return ChatService(graph,ForbiddenMemory(),self.repo,AgentMetrics(),runtime)

    async def run_turn(self,identifier=None,resume=False,identity=None,service=None):
        service=service or self.service;identity=identity or self.alice
        request=ChatRequest(message='question '+uuid4().hex,session_id=identifier)
        p=service.prepare(request,identity)
        async with AsyncExitStack() as resources:
            await service.conversations.prepare(request,p,resources,resume=resume)
            events=[e async for e in service.events(request,p)]
        return p,events

    async def test_multiturn_durable_history_and_server_generated_thread(self):
        p,events=await self.run_turn();self.assertEqual(events[-1]['type'],'complete')
        self.assertNotEqual(p.thread_id,p.session_id)
        p2,events=await self.run_turn(p.session_id)
        self.assertEqual(p2.thread_id,p.thread_id)
        history=await self.service.conversations.history(p.session_id,self.alice)
        self.assertEqual([m['sequence'] for m in history['messages']],[1,2,3,4])
        self.assertNotIn('PRIVATE_DRAFT',str(history))
        snapshot=await self.workflow.aget_state({'configurable':{'thread_id':p.thread_id}})
        self.assertEqual(sum(isinstance(m,HumanMessage) for m in snapshot.values['messages']),2)
        self.assertTrue(history['can_continue']);self.assertFalse(history['can_resume'])

    async def test_pending_node_recovers_after_new_pool_without_duplicate_messages(self):
        self.fail=True;p,events=await self.run_turn()
        self.assertEqual(events[-1]['type'],'error');self.assertEqual(self.calls['intent_router'],1)
        await self.store.close();await self.db.close()
        self.db=Database(self.url,schema=self.schema);self.repo=DatabaseRunRepository(self.db)
        self.store=await PostgresCheckpointStore(self.db).open();self.fail=False
        self.workflow=self.graph();self.service=self.build(self.workflow)
        resumed,events=await self.run_turn(p.session_id,resume=True)
        self.assertEqual(events[-1]['type'],'complete',events[-1])
        self.assertEqual(self.calls['intent_router'],1);self.assertEqual(self.calls['compliance_check'],2)
        history=await self.service.conversations.history(p.session_id,self.alice)
        self.assertEqual(len(history['messages']),2);self.assertEqual(resumed.turn_id,p.turn_id)
        old=await self.repo.get(p.run_id,'alice');new=await self.repo.get(resumed.run_id,'alice')
        self.assertEqual(old['status'],'failed');self.assertEqual(new['resumed_from_run_id'],p.run_id)

    async def test_finished_checkpoint_before_publish_recovers_without_reexecuting(self):
        request=ChatRequest(message='crash window');p=self.service.prepare(request,self.alice)
        async with AsyncExitStack() as stack:
            await self.service.conversations.prepare(request,p,stack)
            token=active_saver.set(p.execution_saver)
            try:
                await self.workflow.ainvoke({'messages':[HumanMessage(id=p.user_message_id,content=request.message)],'turn_id':p.turn_id},
                    {'configurable':{'thread_id':p.thread_id}},durability='sync')
            finally:active_saver.reset(token)
        calls=dict(self.calls)
        resumed,events=await self.run_turn(p.session_id,resume=True)
        self.assertEqual(events[-1]['type'],'complete');self.assertEqual(calls,self.calls)
        old=await self.repo.get(p.run_id,'alice');self.assertEqual(old['error_code'],'EXECUTION_INTERRUPTED')
        history=await self.service.conversations.history(p.session_id,self.alice);self.assertEqual(len(history['messages']),2)
        with self.assertRaisesRegex(ApplicationError,'TURN_ALREADY_COMPLETED'):await self.run_turn(p.session_id,resume=True)

    async def test_crash_before_first_checkpoint_restarts_same_turn(self):
        request=ChatRequest(message='not yet started');p=self.service.prepare(request,self.alice)
        async with AsyncExitStack() as stack:await self.service.conversations.prepare(request,p,stack)
        resumed,events=await self.run_turn(p.session_id,resume=True)
        self.assertEqual(events[-1]['type'],'complete');self.assertEqual(p.user_message_id,resumed.user_message_id)
        self.assertEqual(len((await self.service.conversations.history(p.session_id,self.alice))['messages']),2)

    async def test_new_turn_crash_does_not_publish_previous_turn_answer(self):
        first,_=await self.run_turn();calls=dict(self.calls)
        request=ChatRequest(message='new question before input checkpoint',session_id=first.session_id)
        p=self.service.prepare(request,self.alice)
        async with AsyncExitStack() as stack:await self.service.conversations.prepare(request,p,stack)
        resumed,events=await self.run_turn(first.session_id,resume=True)
        self.assertEqual(events[-1]['type'],'complete');self.assertEqual(resumed.turn_id,p.turn_id)
        self.assertEqual(self.calls['intent_router'],calls['intent_router']+1)
        self.assertEqual(len((await self.service.conversations.history(first.session_id,self.alice))['messages']),4)

    async def test_foreign_subject_cannot_inspect_or_resume_and_body_forbids_checkpoint(self):
        self.fail=True;p,_=await self.run_turn()
        for action in [self.service.conversations.history(p.session_id,self.bob),self.run_turn(p.session_id,resume=True,identity=self.bob)]:
            with self.assertRaisesRegex(ApplicationError,'SESSION_NOT_FOUND'):await action
        self.assertIsNone(await self.repo.get(p.run_id,'bob'))
        from pydantic import ValidationError
        for payload in [{'thread_id':p.thread_id},{'checkpoint_id':'x'},{'state':{}},{'user_id':'alice'}]:
            with self.assertRaises(ValidationError):ResumeRequest.model_validate(payload)

    async def test_concurrent_session_requests_fail_before_acceptance(self):
        p,_=await self.run_turn()
        async with self.store.lock(p.thread_id):
            with self.assertRaisesRegex(ApplicationError,'SESSION_BUSY'):await self.run_turn(p.session_id)
        self.assertEqual(len((await self.service.conversations.history(p.session_id,self.alice))['messages']),2)

    async def test_unfinished_turn_cannot_accept_new_input(self):
        self.fail=True;p,_=await self.run_turn()
        with self.assertRaisesRegex(ApplicationError,'TURN_RECOVERY_REQUIRED'):await self.run_turn(p.session_id)
        self.assertEqual(len((await self.service.conversations.history(p.session_id,self.alice))['messages']),1)

    async def test_recovery_refuses_role_model_version_and_write_capability_changes(self):
        self.fail=True;p,_=await self.run_turn()
        for service,identity,code in [(self.service,self.alice.model_copy(update={'role':'support'}),'RECOVERY_AUTHORIZATION_CHANGED'),
            (self.build(self.graph(models={'intent_router':'changed'})),self.alice,'RECOVERY_MODELS_CHANGED'),
            (self.build(self.graph(writes=True)),self.alice,'RECOVERY_WRITES_DISABLED_REQUIRED')]:
            with self.assertRaisesRegex(ApplicationError,code):await self.run_turn(p.session_id,resume=True,service=service,identity=identity)
        async with self.db.sessions.begin() as s:await s.execute(update(ChatSession).where(ChatSession.id==p.session_id).values(graph_version='future'))
        with self.assertRaisesRegex(ApplicationError,'GRAPH_VERSION_INCOMPATIBLE'):await self.run_turn(p.session_id,resume=True)

    async def test_recovery_refuses_changed_document_permissions_or_content(self):
        self.fail=True;p,_=await self.run_turn()
        identifier='doc-'+uuid4().hex
        async with self.db.sessions.begin() as s:
            s.add(KnowledgeDocument(id=identifier,source='new.txt',title='new',version='v1',domain='consumer_reference',
                status='published',content='new',content_hash='b'*64,effective_from='2026-11-14',access_scope='private',owner_id='alice'))
        with self.assertRaisesRegex(ApplicationError,'RECOVERY_CONTEXT_CHANGED'):await self.run_turn(p.session_id,resume=True)

    async def test_blocked_answer_and_paginated_history_publish_only_safe_output(self):
        self.block=True;p,events=await self.run_turn()
        self.assertFalse(events[-1]['compliance_passed'])
        page=await self.service.conversations.history(p.session_id,self.alice,limit=1)
        self.assertEqual(page['messages'][0]['content'],'safe blocked');self.assertTrue(page['has_more'])
        older=await self.service.conversations.history(p.session_id,self.alice,limit=1,before=page['before_sequence'])
        self.assertEqual(older['messages'][0]['role'],'user');self.assertFalse(older['has_more'])

    async def test_official_serializer_preserves_messages_without_pickle(self):
        value={'messages':[HumanMessage(content='human'),AIMessage(content='final')]}
        serde=serializer();restored=serde.loads_typed(serde.dumps_typed(value))
        self.assertIsInstance(restored['messages'][0],HumanMessage)
        self.assertFalse(serde.pickle_fallback)

    async def test_production_graph_refreshes_trusted_binding_at_resume(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from smart_cs.services.tickets import AgentTicketReader
        model=FakeListChatModel(responses=['{"primary_intent":"unknown","confidence":0.1}','{"passed":true,"risk_level":"low"}'])
        graph=create_supervisor_graph(llm=model,ticket_store=AgentTicketReader(None),checkpointer=self.store.saver)
        service=self.build(graph);p,events=await self.run_turn(service=service)
        self.assertEqual(events[-1]['type'],'complete',events[-1])
        snapshot=await graph.aget_state({'configurable':{'thread_id':p.thread_id}})
        self.assertEqual(snapshot.values['run_id'],p.run_id)
        self.assertNotIn('auth_session_id',snapshot.values['identity_context'])
