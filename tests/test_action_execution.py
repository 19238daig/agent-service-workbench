"""Exactly-once local database effects, consent fencing and post-commit recovery."""
import asyncio
import copy
import json
import os
import subprocess
import sys
import unittest
from contextlib import AsyncExitStack
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from pathlib import Path
import httpx
from sqlalchemy import select,update,func
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from test_approvals import ApprovalFixture
from test_compliance_reviews import ALLOW_ACTION,ALLOW_CONTENT,BLOCK_CONTENT
from smart_cs.db.models import ActionRequest,ActionExecution,Approval,Order,Ticket,TicketEvent
from smart_cs.schemas.execution import ExecutionRequest
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.chat import ChatRequest
from smart_cs.schemas.tickets import TicketTransition
from smart_cs.services.execution import ActionExecutionService,execution_text
from smart_cs.services.compliance import ComplianceReviewService,release_allowed
from smart_cs.services.access import principal_context
from smart_cs.services.chat import ChatService
from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.persistence.conversations import ConversationRuntime
from smart_cs.persistence.postgres import PostgresCheckpointStore,MemoryCheckpointLocks,prepare_checkpoint_store
from smart_cs.repositories.runs import DatabaseRunRepository
from smart_cs.tracing.otel_config import AgentMetrics


class ExecutionTests(ApprovalFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.executor=ActionExecutionService(self.db,clock=lambda:self.now)

    async def approved(self,*,staff=False,actor=None,body=None):
        actor=actor or self.a
        state=await self.draft(staff=staff,actor=actor,body=body);view=await self.approvals.create(actor,state)
        await self.approvals.decide(actor,view['action_id'],self.decision(view))
        if view['required_approval']=='staff':
            reviewer=self.staff if actor.subject_id!=self.staff.subject_id else self.admin
            await self.approvals.decide(reviewer,view['action_id'],self.decision(view),staff=True)
        return view

    async def execute(self,view,actor=None):
        return await self.executor.execute(actor or self.a,view['action_id'],view['parameter_hash'])

    async def counts(self):
        async with self.db.sessions() as s:
            order=await s.get(Order,self.order)
            return {'tickets':await s.scalar(select(func.count()).select_from(Ticket)),
                'events':await s.scalar(select(func.count()).select_from(TicketEvent)),
                'receipts':await s.scalar(select(func.count()).select_from(ActionExecution)),
                'reserved':str(order.refund_reserved_amount),'refunded':str(order.refunded_amount),'version':order.fact_version}

    async def test_refund_commit_has_one_ticket_event_receipt_and_reservation(self):
        before=await self.counts();view=await self.approved();result=await self.execute(view);after=await self.counts()
        self.assertEqual(result['status'],'succeeded');self.assertFalse(result['funds_refunded'])
        self.assertEqual(after['tickets'],before['tickets']+1);self.assertEqual(after['events'],before['events']+1)
        self.assertEqual(after['receipts'],before['receipts']+1);self.assertEqual(after['reserved'],'149.99');self.assertEqual(after['version'],4)
        self.assertEqual(after['refunded'],before['refunded']);self.assertEqual(result['reservation_change'],'100.00')
        async with self.db.sessions() as s:
            ticket=await s.get(Ticket,result['ticket_id']);self.assertEqual(str(ticket.requested_amount),'100.00')
            self.assertEqual((await s.get(TicketEvent,result['event_id'])).action_id,view['action_id'])
            self.assertEqual((await s.get(ActionRequest,view['action_id'])).status,'executed')

    async def test_retry_after_expiry_or_later_business_change_returns_original_receipt(self):
        view=await self.approved();first=await self.execute(view);before=await self.counts();self.now+=timedelta(days=1)
        async with self.db.sessions.begin() as s:await s.execute(update(Ticket).where(Ticket.id==first['ticket_id']).values(summary='后续人工备注'))
        self.assertEqual(await self.execute(view),first);self.assertEqual(await self.executor.get(self.a,view['action_id']),first)
        self.assertEqual(before,await self.counts())
        self.assertTrue((await self.approvals.resume_check(self.a,view['action_id']))['executed'])

    async def test_unconfirmed_or_forged_status_cannot_execute(self):
        view=await self.approvals.create(self.a,await self.draft());before=await self.counts()
        for forged in [False,True]:
            if forged:
                async with self.db.sessions.begin() as s:await s.execute(update(ActionRequest).where(ActionRequest.id==view['action_id']).values(status='approved'))
            with self.assertRaisesRegex(ApplicationError,'APPROVAL_REQUIRED'):await self.execute(view)
        self.assertEqual(before,await self.counts())

    async def test_expired_changed_hash_or_foreign_actor_cannot_mutate(self):
        view=await self.approved();before=await self.counts()
        with self.assertRaisesRegex(ApplicationError,'ACTION_BINDING_MISMATCH'):await self.executor.execute(self.a,view['action_id'],'f'*64)
        with self.assertRaisesRegex(ApplicationError,'ACTION_NOT_FOUND'):await self.execute(view,self.b)
        self.now+=timedelta(minutes=6)
        with self.assertRaisesRegex(ApplicationError,'APPROVAL_EXPIRED'):await self.execute(view)
        self.assertEqual(before,await self.counts())

    async def test_changed_business_version_blocks_old_approval(self):
        view=await self.approved()
        async with self.db.sessions.begin() as s:await s.execute(update(Order).where(Order.id==self.order).values(fact_version=4))
        before=await self.counts()
        with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):await self.execute(view)
        self.assertEqual(before,await self.counts())

    async def test_tampered_snapshot_or_approval_binding_never_writes(self):
        for kind in ['draft','approval','effects']:
            view=await self.approved();before=await self.counts()
            async with self.db.sessions.begin() as s:
                if kind in {'draft','effects'}:
                    row=await s.get(ActionRequest,view['action_id']);data=copy.deepcopy(row.snapshot)
                    if kind=='draft':data['action_plan']['parameters']['requested_amount']='99.00'
                    else:data['action_plan']['effects']['refund_reserved_delta']='999.00'
                    row.snapshot=data
                else:await s.execute(update(Approval).where(Approval.action_id==view['action_id']).values(draft_hash='f'*64))
            with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):await self.execute(view)
            self.assertEqual(before,await self.counts())

    async def test_missing_staff_approval_blocks_high_risk_write(self):
        view=await self.approvals.create(self.a,await self.draft(staff=True));await self.approvals.decide(self.a,view['action_id'],self.decision(view))
        before=await self.counts()
        with self.assertRaisesRegex(ApplicationError,'APPROVAL_REQUIRED'):await self.execute(view)
        self.assertEqual(before,await self.counts())

    async def test_precommit_failure_rolls_back_ticket_event_receipt_and_amount(self):
        view=await self.approved();before=await self.counts()
        async def fault(stage):
            if stage=='before_commit':raise RuntimeError('injected precommit failure')
        executor=ActionExecutionService(self.db,clock=lambda:self.now,fault=fault)
        with self.assertRaises(RuntimeError):await executor.execute(self.a,view['action_id'],view['parameter_hash'])
        self.assertEqual(before,await self.counts())
        self.assertEqual((await self.approvals.get(self.a,view['action_id']))['status'],'approved')
        self.assertEqual((await self.execute(view))['status'],'succeeded')

    async def test_after_commit_failure_leaves_queryable_result_and_retry_is_read_only(self):
        view=await self.approved()
        async def fault(stage):
            if stage=='after_commit':raise RuntimeError('injected response loss')
        with self.assertRaises(RuntimeError):await ActionExecutionService(self.db,clock=lambda:self.now,fault=fault).execute(self.a,view['action_id'],view['parameter_hash'])
        result=await self.executor.get(self.a,view['action_id']);before=await self.counts()
        self.assertEqual(await self.execute(view),result);self.assertEqual(before,await self.counts())

    async def test_cancellation_before_and_after_commit_preserves_correct_outcome(self):
        for stage in ['before_commit','after_commit']:
            view=await self.approved();before=await self.counts()
            async def fault(actual):
                if actual==stage:raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):await ActionExecutionService(self.db,clock=lambda:self.now,fault=fault).execute(self.a,view['action_id'],view['parameter_hash'])
            if stage=='before_commit':self.assertEqual(before,await self.counts())
            else:self.assertEqual((await self.executor.get(self.a,view['action_id']))['status'],'succeeded')

    async def test_staff_transition_commits_once_and_stale_second_action_fails(self):
        first=await self.approved(actor=self.staff);second=await self.approved(actor=self.staff)
        result=await self.execute(first,self.staff);self.assertEqual(result['ticket_version'],3);self.assertEqual(result['ticket_status'],'resolved')
        self.assertEqual(await self.execute(first,self.staff),result)
        with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):await self.execute(second,self.staff)

    async def test_closing_tracked_refund_releases_only_its_reserved_amount(self):
        created=await self.execute(await self.approved())
        body=self.plan('ticket_transition',ticket_id=created['ticket_id'],target_status='closed',expected_version=1,reason='撤销申请')
        closing=await self.approved(body=body);result=await self.execute(closing)
        self.assertEqual(result['reservation_change'],'-100.00');self.assertEqual(result['ticket_status'],'closed')
        counts=await self.counts();self.assertEqual(counts['reserved'],'49.99');self.assertEqual(counts['version'],5)
        self.assertEqual(await self.execute(closing),result);self.assertEqual(counts,await self.counts())

    async def test_manual_transition_of_action_managed_ticket_requires_protected_path(self):
        created=await self.execute(await self.approved())
        with self.assertRaisesRegex(ApplicationError,'ACTION_APPROVAL_REQUIRED'):
            await self.service.tickets.transition(self.a,created['ticket_id'],TicketTransition(target_status='closed',expected_version=1,reason='撤销'))

    async def test_receipt_corruption_is_not_reexecuted_or_reported_as_success(self):
        view=await self.approved();await self.execute(view);before=await self.counts()
        async with self.db.sessions.begin() as s:await s.execute(update(ActionExecution).where(ActionExecution.action_id==view['action_id']).values(result_hash='f'*64))
        for request in [self.execute(view),self.executor.get(self.a,view['action_id'])]:
            with self.assertRaisesRegex(ApplicationError,'EXECUTION_RECEIPT_INVALID'):await request
        self.assertEqual(before,await self.counts())

    async def test_result_review_requires_database_proof_and_exact_server_text(self):
        view=await self.approved();result=await self.execute(view);state={'action_execution':result}
        token=principal_context.set(self.a)
        class Model:
            async def ainvoke(self,*args):
                from types import SimpleNamespace
                return SimpleNamespace(content=json.dumps(ALLOW_CONTENT))
        try:
            service=ComplianceReviewService(Model(),execution_service=self.executor)
            self.assertEqual((await service.review_content(execution_text(result),state)).decision,'allow')
            self.assertEqual((await service.review_content('已退款到账。',state)).decision,'block')
            altered={**result,'ticket_status':'resolved'}
            self.assertEqual((await service.review_content(execution_text(altered),{'action_execution':altered})).decision,'block')
        finally:principal_context.reset(token)

    async def test_http_execution_is_owner_scoped_strict_and_returns_committed_result(self):
        from smart_cs.api import main
        from smart_cs.api.dependencies import current_identity
        view=await self.approved();url='/api/action-requests/'+view['action_id'];original=dict(main.app.dependency_overrides)
        try:
            with patch.object(main,'execution_service',return_value=self.executor):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
                    main.app.dependency_overrides[current_identity]=lambda:self.a
                    self.assertEqual((await client.get(url+'/execution')).status_code,404)
                    for extra in [{'approved':True},{'parameters':{}},{'actor_id':self.a.subject_id},{'fault':'after_commit'}]:
                        self.assertEqual((await client.post(url+'/execute',json={'parameter_hash':view['parameter_hash'],**extra})).status_code,422)
                    first=await client.post(url+'/execute',json={'parameter_hash':view['parameter_hash']});self.assertEqual(first.status_code,200)
                    self.assertEqual(first.headers['Cache-Control'],'no-store')
                    self.assertEqual((await client.get(url+'/execution')).json(),first.json())
                    self.assertEqual((await client.post(url+'/execute',json={'parameter_hash':view['parameter_hash']})).json(),first.json())
                    main.app.dependency_overrides[current_identity]=lambda:self.b
                    self.assertEqual((await client.get(url+'/execution')).status_code,404)
                    main.app.dependency_overrides.pop(current_identity)
                    self.assertEqual((await client.get(url+'/execution')).status_code,401)
        finally:main.app.dependency_overrides.clear();main.app.dependency_overrides.update(original)

    @unittest.skipUnless(os.getenv('AUTH_VERIFY_SCHEMA'),'Requires PostgreSQL row locks')
    async def test_parallel_same_action_has_exactly_one_committed_effect(self):
        view=await self.approved();before=await self.counts()
        results=await asyncio.gather(*(self.execute(view) for _ in range(8)))
        self.assertTrue(all(r==results[0] for r in results));after=await self.counts()
        self.assertEqual(after['tickets'],before['tickets']+1);self.assertEqual(after['receipts'],before['receipts']+1);self.assertEqual(after['events'],before['events']+1)

    @unittest.skipUnless(os.getenv('AUTH_VERIFY_SCHEMA'),'Requires PostgreSQL row locks')
    async def test_parallel_different_actions_cannot_reserve_against_the_same_old_balance(self):
        body=self.plan(requested_amount='200.00');a=await self.approved(body=body);b=await self.approved(body=body)
        results=await asyncio.gather(self.execute(a),self.execute(b),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(sum(isinstance(r,ApplicationError) for r in results),1)
        self.assertEqual((await self.counts())['reserved'],'249.99')

    @unittest.skipUnless(os.getenv('AUTH_VERIFY_SCHEMA'),'Requires restricted PostgreSQL process probe')
    async def test_process_exit_after_commit_restarts_and_reads_same_result(self):
        view=await self.approved();before=await self.counts();env=os.environ.copy();env['EXECUTION_VERIFY_NOW']=self.now.isoformat()
        r=await asyncio.to_thread(subprocess.run,[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/execution_crash_probe.py'),
            view['action_id'],self.a.subject_id,view['parameter_hash']],env=env,capture_output=True,timeout=45)
        self.assertEqual(r.returncode,77)
        result=await self.executor.get(self.a,view['action_id']);self.assertEqual(await self.execute(view),result)
        self.assertEqual((await self.counts())['tickets'],before['tickets']+1)

    async def graph_case(self,*,blocked=False,crash=False,reopen=False):
        if reopen:
            await prepare_checkpoint_store(self.db);store=await PostgresCheckpointStore(self.db).open()
        else:store=MemoryCheckpointLocks()
        def graph(executor,*,recovering=False):
            route=json.dumps({'primary_intent':'transaction','confidence':.99,'secondary_intent':'ticket_create','suggested_agent':'ticket_handler'})
            models={role:FakeListChatModel(responses=values) for role,values in {
                'intent_router':[route],'ticket_handler':[self.plan().model_dump_json()],'knowledge_rag':['unused'],
                'compliance_checker':([json.dumps(ALLOW_CONTENT)] if recovering else [json.dumps(ALLOW_ACTION),json.dumps(ALLOW_CONTENT),json.dumps(BLOCK_CONTENT if blocked else ALLOW_CONTENT)])}.items()}
            if reopen:checkpoint=store.saver
            else:
                from langgraph.checkpoint.memory import MemorySaver
                checkpoint=MemorySaver()
            workflow=create_supervisor_graph(agent_llms=models,tool_service=self.service,approval_service=self.approvals,
                execution_service=executor,checkpointer=checkpoint)
            repo=DatabaseRunRepository(self.db)
            return ChatService(workflow,None,repo,AgentMetrics(),ConversationRuntime(self.db,store,workflow,repo))
        async def fault(stage):
            if stage=='after_commit':raise RuntimeError('lost response after commit')
        service=graph(ActionExecutionService(self.db,clock=lambda:self.now,fault=fault) if crash else self.executor)
        async def run(identifier=None,resume=False):
            request=ChatRequest(message=f'本人订单 {self.order} 申请退款100元，原因不合适，请生成待确认草稿。',session_id=identifier);p=service.prepare(request,self.a)
            async with AsyncExitStack() as resources:
                await service.conversations.prepare(request,p,resources,resume=resume)
                with patch('smart_cs.services.execution.ActionExecutionService',return_value=self.executor),patch('smart_cs.services.actions.ActionPlanService',return_value=self.service):
                    events=[e async for e in service.events(request,p)]
            return p,events
        try:
            with patch('smart_cs.services.approvals.ApprovalService',return_value=self.approvals):
                p,events=await run();self.assertEqual(events[-1]['type'],'complete',events[-1]);view=events[-1]['action_request']
                self.assertEqual(view['status'],'awaiting_user');await self.approvals.decide(self.a,view['action_id'],self.decision(view))
                _,events=await run(p.session_id,True)
                if crash:
                    self.assertEqual(events[-1]['type'],'error');self.assertEqual(events[-1]['action_execution']['status'],'succeeded')
                    from dataclasses import replace
                    from unittest.mock import AsyncMock
                    with patch.object(service.workflow,'aget_state',new=AsyncMock(return_value=None)):
                        with self.assertRaisesRegex(ApplicationError,'CHECKPOINT_EXECUTION_MISMATCH'):
                            await service.conversations.graph_input(replace(p,kind='resume'),{}, {'configurable':{'thread_id':p.thread_id}})
                    before=await self.counts();self.now+=timedelta(minutes=10)
                    if reopen:
                        await store.close();store=await PostgresCheckpointStore(self.db).open()
                    service=graph(self.executor,recovering=True)
                    # Restarted reviewer may use new model settings; the committed tail is safe to recover.
                    _,events=await run(p.session_id,True);self.assertEqual(before,await self.counts())
                final=events[-1];self.assertEqual(final['type'],'complete',final)
                self.assertEqual(final['action_execution']['status'],'succeeded')
                self.assertEqual(final['compliance_passed'],not blocked)
                history=await service.conversations.history(p.session_id,self.a)
                self.assertEqual(history['messages'][-1]['metadata']['action_execution'],final['action_execution'])
                self.assertEqual(await self.executor.get(self.a,view['action_id']),final['action_execution'])
        finally:await store.close()

    async def test_full_graph_executes_and_reviews_real_receipt(self):await self.graph_case()

    async def test_post_commit_review_block_does_not_hide_or_roll_back_receipt(self):await self.graph_case(blocked=True)

    @unittest.skipUnless(os.getenv('AUTH_VERIFY_SCHEMA'),'Requires actual PostgreSQL checkpoint reopening')
    async def test_graph_commit_then_disconnect_survives_restart_and_expired_approval(self):await self.graph_case(crash=True,reopen=True)
