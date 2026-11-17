"""Consent adversarial cases on SQLite and restricted PostgreSQL; no model network."""
import asyncio
import copy
import json
import os
import unittest
from contextlib import AsyncExitStack
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch
import httpx
from sqlalchemy import select, update, func
from langgraph.graph import StateGraph, END
from langgraph.types import Command
from langgraph.checkpoint.memory import MemorySaver
from test_action_plans import ActionPlanFixture
from test_compliance_reviews import Model, ALLOW_ACTION, ALLOW_CONTENT
from smart_cs.db.models import ChatSession, ChatMessage, ChatTurn, ExecutionRun, ActionRequest, Approval, Order, User
from smart_cs.services.approvals import ApprovalService, ApprovalGate
from smart_cs.services.compliance import ComplianceReviewService, candidate_text
from smart_cs.services.actions import format_plan
from smart_cs.schemas.actions import PlanValidation
from smart_cs.schemas.approvals import HumanDecision, ActionCancellation
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.chat import ChatRequest
from smart_cs.agents.supervisor import AgentState
from smart_cs.services.chat import ChatService
from smart_cs.persistence.conversations import ConversationRuntime
from smart_cs.persistence.context import GRAPH_VERSION, execution_binding
from smart_cs.persistence.postgres import MemoryCheckpointLocks, PostgresCheckpointStore, prepare_checkpoint_store
from smart_cs.repositories.runs import DatabaseRunRepository
from smart_cs.tracing.otel_config import AgentMetrics


class ApprovalFixture(ActionPlanFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from test_action_plans import NOW
        self.now=NOW
        self.approvals=ApprovalService(self.db,self.service,clock=lambda:self.now)

    async def draft(self, *, staff=False, actor=None,body=None):
        actor=actor or self.a; identifier=uuid4().hex; turn=uuid4().hex; run=uuid4().hex; message=uuid4().hex
        async with self.db.sessions.begin() as s:
            s.add(ChatSession(id=identifier,user_id=actor.subject_id,thread_id=uuid4().hex,graph_version=GRAPH_VERSION));await s.flush()
            s.add(ExecutionRun(id=run,request_id=uuid4().hex,trace_id=uuid4().hex,session_id=identifier,subject_id=actor.subject_id,status='running',snapshot={}));await s.flush()
            s.add(ChatMessage(id=message,session_id=identifier,user_id=actor.subject_id,role='user',sequence=1,content='fixture',run_id=run));await s.flush()
            s.add(ChatTurn(id=turn,session_id=identifier,user_id=actor.subject_id,current_run_id=run,user_message_id=message,
                graph_version=GRAPH_VERSION,authorization_role=actor.role,context_fingerprint='a'*64,model_fingerprint='b'*64,status='running'))
        if body is None:
            if actor.role=='customer': body=self.plan(priority='urgent' if staff else 'medium')
            else: body=self.plan('ticket_transition',ticket_id=self.ticket,target_status='resolved',expected_version=2,reason='核实资料')
        plan=(await self.service.validate(actor,body,binding=turn)).model_dump(mode='json')
        reviews=ComplianceReviewService(Model(ALLOW_ACTION,ALLOW_CONTENT),self.service)
        review=(await reviews.review_action(actor,plan)).model_dump(mode='json')
        state={'session_id':identifier,'turn_id':turn,'run_id':run,'action_plan':plan,'action_review':review,
            'sub_results':{'ticket':format_plan(PlanValidation.model_validate(plan),approval=review['required_approval'])},'knowledge_evidence':{},'compliance_passed':True}
        state['content_review']=(await reviews.review_content(candidate_text(state),state)).model_dump(mode='json')
        state['final_response']=candidate_text(state)
        return state

    def decision(self,view,decision='approve'):
        return HumanDecision(parameter_hash=view['parameter_hash'],decision=decision)


class ApprovalTests(ApprovalFixture, unittest.IsolatedAsyncioTestCase):

    async def test_normal_confirmation_is_durable_idempotent_and_has_no_business_effect(self):
        before=await self.facts();state=await self.draft();view=await self.approvals.create(self.a,state)
        self.assertEqual(view['status'],'awaiting_user')
        with self.assertRaisesRegex(ApplicationError,'APPROVAL_REQUIRED'):await self.approvals.resume_check(self.a,view['action_id'])
        decided=await self.approvals.decide(self.a,view['action_id'],self.decision(view))
        self.assertEqual(decided['status'],'approved')
        self.assertEqual((await self.approvals.decide(self.a,view['action_id'],self.decision(view)))['approvals'],decided['approvals'])
        ready=await self.approvals.resume_check(self.a,view['action_id'],mark_ready=True)
        self.assertEqual(ready['status'],'ready');self.assertFalse(ready['executed']);self.assertFalse(ready['execution_enabled'])
        recreated=ApprovalService(self.db,self.service,clock=lambda:self.now)
        self.assertEqual((await recreated.get(self.a,view['action_id']))['approvals'],decided['approvals'])
        self.assertEqual(before,await self.facts())

    async def test_staff_review_needs_requester_confirmation_and_distinct_reviewer(self):
        view=await self.approvals.create(self.a,await self.draft(staff=True));identifier=view['action_id']
        self.assertNotIn(identifier,[r['action_id'] for r in await self.approvals.list(self.staff,staff=True)])
        with self.assertRaisesRegex(ApplicationError,'ACTION_NOT_FOUND'):await self.approvals.decide(self.staff,identifier,self.decision(view),staff=True)
        self.assertEqual((await self.approvals.decide(self.a,identifier,self.decision(view)))['status'],'awaiting_staff')
        self.assertIn(identifier,[r['action_id'] for r in await self.approvals.list(self.staff,staff=True)])
        with self.assertRaisesRegex(ApplicationError,'APPROVAL_REQUIRED'):await self.approvals.resume_check(self.a,identifier)
        self.assertEqual((await self.approvals.decide(self.staff,identifier,self.decision(view),staff=True))['status'],'approved')
        self.assertEqual((await self.approvals.resume_check(self.a,identifier,mark_ready=True))['status'],'ready')
        second=await self.approvals.create(self.staff,await self.draft(staff=True,actor=self.staff))
        await self.approvals.decide(self.staff,second['action_id'],self.decision(second))
        with self.assertRaisesRegex(ApplicationError,'SELF_APPROVAL_FORBIDDEN'):await self.approvals.decide(self.staff,second['action_id'],self.decision(second),staff=True)

    async def test_foreign_identity_role_spoof_and_hash_changes_are_rejected(self):
        view=await self.approvals.create(self.a,await self.draft(staff=True));identifier=view['action_id']
        for fn in [self.approvals.get(self.b,identifier),self.approvals.decide(self.b,identifier,self.decision(view)),self.approvals.resume_check(self.b,identifier)]:
            with self.assertRaisesRegex(ApplicationError,'ACTION_NOT_FOUND'):await fn
        with self.assertRaisesRegex(ApplicationError,'ACTION_BINDING_MISMATCH'):
            await self.approvals.decide(self.a,identifier,HumanDecision(parameter_hash='f'*64,decision='approve'))
        await self.approvals.decide(self.a,identifier,self.decision(view))
        with self.assertRaisesRegex(ApplicationError,'AUTHORIZATION_CHANGED'):
            await self.approvals.decide(self.b.model_copy(update={'role':'support'}),identifier,self.decision(view),staff=True)

    async def test_rejection_cancellation_and_decision_reversal_never_resume(self):
        for rejection in [True,False]:
            view=await self.approvals.create(self.a,await self.draft());identifier=view['action_id']
            if rejection: await self.approvals.decide(self.a,identifier,self.decision(view,'reject'))
            else: await self.approvals.cancel(self.a,identifier,ActionCancellation(parameter_hash=view['parameter_hash']))
            with self.assertRaisesRegex(ApplicationError,'ACTION_REJECTED|ACTION_CANCELLED'):await self.approvals.resume_check(self.a,identifier)

    async def test_request_and_approval_expiry_are_persisted_and_fail_closed(self):
        for approve,minutes in [(False,16),(True,6)]:
            view=await self.approvals.create(self.a,await self.draft())
            if approve:await self.approvals.decide(self.a,view['action_id'],self.decision(view))
            self.now+=timedelta(minutes=minutes)
            with self.assertRaisesRegex(ApplicationError,'ACTION_EXPIRED|APPROVAL_EXPIRED'):await self.approvals.resume_check(self.a,view['action_id'])
            async with self.db.sessions() as s:self.assertEqual((await s.get(ActionRequest,view['action_id'])).status,'expired')

    async def test_changed_business_facts_or_policy_invalidate_confirmation(self):
        view=await self.approvals.create(self.a,await self.draft())
        await self.approvals.decide(self.a,view['action_id'],self.decision(view))
        async with self.db.sessions.begin() as s:await s.execute(update(Order).where(Order.id==self.order).values(fact_version=4))
        with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):await self.approvals.resume_check(self.a,view['action_id'])
        state=await self.draft();view=await self.approvals.create(self.a,state)
        with patch('smart_cs.services.compliance.policy_fingerprint',return_value='changed'):
            with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):await self.approvals.decide(self.a,view['action_id'],self.decision(view))

    async def test_tampered_draft_cannot_be_shown_confirmed_or_replayed(self):
        state=await self.draft();view=await self.approvals.create(self.a,state)
        self.assertEqual((await self.approvals.create(self.a,state))['action_id'],view['action_id'])
        changed=copy.deepcopy(state);changed['action_plan']['parameters']['requested_amount']='1.00'
        with self.assertRaisesRegex(ApplicationError,'UNREVIEWED_DRAFT'):await self.approvals.create(self.a,changed)
        async with self.db.sessions.begin() as s:
            row=await s.get(ActionRequest,view['action_id']);row.snapshot=changed
        with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):await self.approvals.decide(self.a,view['action_id'],self.decision(view))
        self.assertEqual((await self.approvals.get(self.a,view['action_id']))['parameters'],{})

    async def test_forged_approved_status_or_approval_hash_cannot_resume(self):
        view=await self.approvals.create(self.a,await self.draft())
        async with self.db.sessions.begin() as s:await s.execute(update(ActionRequest).where(ActionRequest.id==view['action_id']).values(status='approved'))
        with self.assertRaisesRegex(ApplicationError,'APPROVAL_REQUIRED'):await self.approvals.resume_check(self.a,view['action_id'])
        async with self.db.sessions.begin() as s:await s.execute(update(ActionRequest).where(ActionRequest.id==view['action_id']).values(status='awaiting_user'))
        await self.approvals.decide(self.a,view['action_id'],self.decision(view))
        async with self.db.sessions.begin() as s:await s.execute(update(Approval).where(Approval.action_id==view['action_id']).values(parameter_hash='f'*64))
        with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):await self.approvals.resume_check(self.a,view['action_id'])

    async def test_reviewer_role_change_revokes_approval(self):
        view=await self.approvals.create(self.a,await self.draft(staff=True));identifier=view['action_id']
        await self.approvals.decide(self.a,identifier,self.decision(view));await self.approvals.decide(self.staff,identifier,self.decision(view),staff=True)
        async with self.db.sessions.begin() as s:await s.execute(update(User).where(User.id==self.staff.subject_id).values(role='customer'))
        with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):await self.approvals.resume_check(self.a,identifier)

    async def test_http_auth_schema_ownership_and_no_store(self):
        from smart_cs.api import main
        from smart_cs.api.dependencies import current_identity
        view=await self.approvals.create(self.a,await self.draft());identifier=view['action_id']
        original=dict(main.app.dependency_overrides)
        try:
            with patch.object(main,'database',self.db),patch.object(main,'approval_service',return_value=self.approvals):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
                    main.app.dependency_overrides[current_identity]=lambda:self.b
                    self.assertEqual((await client.get('/api/action-requests/'+identifier)).status_code,404)
                    self.assertEqual((await client.get('/api/staff/action-requests')).status_code,403)
                    main.app.dependency_overrides[current_identity]=lambda:self.a
                    url='/api/action-requests/'+identifier+'/confirm';body=self.decision(view).model_dump()
                    for extra in [{'actor_id':self.staff.subject_id},{'approved':True},{'parameters':{}},{'execution_enabled':True},{'expires_at':'2099'}]:
                        self.assertEqual((await client.post(url,json={**body,**extra})).status_code,422)
                    response=await client.post(url,json=body);self.assertEqual(response.status_code,200);self.assertEqual(response.headers['Cache-Control'],'no-store')
                    main.app.dependency_overrides.pop(current_identity)
                    self.assertEqual((await client.post(url,json=body)).status_code,401)
        finally: main.app.dependency_overrides.clear();main.app.dependency_overrides.update(original)

    async def test_langgraph_interrupt_resume_without_repeating_review_or_business_write(self):
        before=await self.facts();state=await self.draft();calls=[]
        graph=StateGraph(AgentState)
        async def reviewed(state): calls.append('reviewed');return state
        graph.add_node('reviewed',reviewed);graph.add_node('approval_gate',ApprovalGate(self.approvals))
        graph.set_entry_point('reviewed');graph.add_edge('reviewed','approval_gate');graph.add_edge('approval_gate',END)
        from smart_cs.services.access import principal_context
        token=principal_context.set(self.a)
        try:
            workflow=graph.compile(checkpointer=MemorySaver());config={'configurable':{'thread_id':uuid4().hex}}
            result=await workflow.ainvoke(state,config,durability='sync');self.assertTrue(result['__interrupt__'])
            view=await self.approvals.for_turn(self.a,state['turn_id'])
            await self.approvals.decide(self.a,view['action_id'],self.decision(view))
            result=await workflow.ainvoke(Command(resume={'action_id':view['action_id']}),config,durability='sync')
            self.assertEqual(result['action_request']['status'],'ready');self.assertEqual(calls,['reviewed'])
        finally:principal_context.reset(token)
        self.assertEqual(before,await self.facts())

    @unittest.skipUnless(os.getenv('AUTH_VERIFY_SCHEMA'),'Requires restricted PostgreSQL saver')
    async def test_postgres_checkpoint_reopens_after_wait_and_resumes_without_replanning(self):
        await prepare_checkpoint_store(self.db);store=await PostgresCheckpointStore(self.db).open();calls=[]
        async def reviewed(state):
            calls.append('reviewed');binding=execution_binding.get();draft=await self.draft()
            # Replace disposable fixture binding with the canonical runtime turn.
            plan=(await self.service.validate(self.a,self.plan(),binding=binding['turn_id'])).model_dump(mode='json')
            reviews=ComplianceReviewService(Model(ALLOW_ACTION,ALLOW_CONTENT),self.service)
            draft={**draft,**binding,'action_plan':plan,'action_review':(await reviews.review_action(self.a,plan)).model_dump(mode='json')}
            draft['sub_results']={'ticket':format_plan(PlanValidation.model_validate(plan),approval='user')}
            draft['content_review']=(await reviews.review_content(candidate_text(draft),draft)).model_dump(mode='json');draft['final_response']=candidate_text(draft)
            return draft
        def build(store):
            graph=StateGraph(AgentState);graph.add_node('synthesize',reviewed);graph.add_node('approval_gate',ApprovalGate(self.approvals))
            graph.set_entry_point('synthesize');graph.add_edge('synthesize','approval_gate');graph.add_edge('approval_gate',END)
            workflow=graph.compile(checkpointer=store.saver).with_config(metadata={'graph_version':GRAPH_VERSION,'agent_writes_enabled':False,'review_protocol':'dual-review-v1','workflow_models':{}})
            repo=DatabaseRunRepository(self.db)
            return ChatService(workflow,None,repo,AgentMetrics(),ConversationRuntime(self.db,store,workflow,repo))
        async def run(service,identifier=None,resume=False):
            request=ChatRequest(message='draft',session_id=identifier);prepared=service.prepare(request,self.a)
            async with AsyncExitStack() as resources:
                await service.conversations.prepare(request,prepared,resources,resume=resume)
                # Final business check uses the fixture clock too.
                with patch('smart_cs.services.actions.ActionPlanService',return_value=self.service):
                    events=[e async for e in service.events(request,prepared)]
            return prepared,events
        try:
            # Runtime checks instantiate the same service with the controlled clock.
            with patch('smart_cs.services.approvals.ApprovalService',return_value=self.approvals):
                service=build(store);p,events=await run(service)
                self.assertEqual(events[-1]['type'],'complete',events[-1]);self.assertEqual(events[-1]['action_request']['status'],'awaiting_user')
                detail=await service.conversations.detail(p.session_id,self.a);self.assertEqual(detail['turn_status'],'waiting_approval');self.assertFalse(detail['can_resume'])
                await store.close();store=await PostgresCheckpointStore(self.db).open();service=build(store)
                view=await self.approvals.for_turn(self.a,p.turn_id)
                with self.assertRaisesRegex(ApplicationError,'APPROVAL_REQUIRED'):await run(service,p.session_id,True)
                await self.approvals.decide(self.a,view['action_id'],self.decision(view))
                _,events=await run(service,p.session_id,True)
                self.assertEqual(events[-1]['type'],'complete',events[-1]);self.assertEqual(events[-1]['action_request']['status'],'ready');self.assertEqual(calls,['reviewed'])
                self.assertTrue((await service.conversations.detail(p.session_id,self.a))['can_continue'])
        finally:await store.close()

    async def test_full_four_agent_graph_preserves_interrupt_through_error_boundary(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from smart_cs.agents.supervisor import create_supervisor_graph
        route=json.dumps({'primary_intent':'transaction','confidence':.99,'secondary_intent':'ticket_create','suggested_agent':'ticket_handler'})
        models={role:FakeListChatModel(responses=values) for role,values in {
            'intent_router':[route],'ticket_handler':[self.plan().model_dump_json()], 'knowledge_rag':['unused'],
            'compliance_checker':[json.dumps(ALLOW_ACTION),json.dumps(ALLOW_CONTENT)]}.items()}
        workflow=create_supervisor_graph(agent_llms=models,tool_service=self.service,approval_service=self.approvals,checkpointer=MemorySaver())
        store=MemoryCheckpointLocks();repo=DatabaseRunRepository(self.db)
        service=ChatService(workflow,None,repo,AgentMetrics(),ConversationRuntime(self.db,store,workflow,repo))
        before=await self.facts()
        async def run(identifier=None,resume=False):
            request=ChatRequest(message=f'为本人订单 {self.order} 申请退款100元，原因不合适，请生成待确认草稿。',session_id=identifier)
            p=service.prepare(request,self.a)
            async with AsyncExitStack() as resources:
                await service.conversations.prepare(request,p,resources,resume=resume)
                with patch('smart_cs.services.actions.ActionPlanService',return_value=self.service):
                    result=[e async for e in service.events(request,p)]
            return p,result
        with patch('smart_cs.services.approvals.ApprovalService',return_value=self.approvals):
            p,result=await run();self.assertEqual(result[-1]['type'],'complete',result[-1])
            view=result[-1]['action_request'];self.assertEqual(view['status'],'awaiting_user')
            initial=await repo.get(p.run_id,self.a.subject_id);self.assertEqual(initial['status'],'waiting_approval')
            await self.approvals.decide(self.a,view['action_id'],self.decision(view))
            resumed,result=await run(p.session_id,True)
            self.assertEqual(result[-1]['type'],'complete',result[-1]);self.assertEqual(result[-1]['action_request']['status'],'ready')
            self.assertEqual((await repo.get(resumed.run_id,self.a.subject_id))['model_calls'],[])
        self.assertEqual(before,await self.facts())

    @unittest.skipUnless(os.getenv('AUTH_VERIFY_SCHEMA'),'Requires PostgreSQL row locking')
    async def test_concurrent_confirmations_record_only_one_decision(self):
        view=await self.approvals.create(self.a,await self.draft())
        results=await asyncio.gather(*(self.approvals.decide(self.a,view['action_id'],self.decision(view)) for _ in range(5)))
        self.assertTrue(all(r['status']=='approved' for r in results))
        async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(Approval).where(Approval.action_id==view['action_id'])),1)
