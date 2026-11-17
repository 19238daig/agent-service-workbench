"""Finite dependencies and persisted clarification; sources/models are explicit fixtures."""
import asyncio
import copy
import json
import os
import unittest
from contextlib import AsyncExitStack
from unittest.mock import patch,AsyncMock
from uuid import uuid4
from sqlalchemy import update,select,func
from langchain_core.messages import HumanMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from test_approvals import ApprovalFixture
from test_compliance_reviews import ALLOW_ACTION,ALLOW_CONTENT,BLOCK_CONTENT
from smart_cs.db.models import Order,Ticket,ActionRequest,ChatSession
from smart_cs.schemas.tasks import TaskProposal,TaskPlan,Task,Slots,public_plan
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.chat import ChatRequest
from smart_cs.services.tasks import TaskOrchestrator,sanitized_slots,wants_write
from smart_cs.services.execution import ActionExecutionService
from smart_cs.services.access import principal_context
from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.agents.intent_router import IntentRouterAgent
from smart_cs.persistence.context import execution_binding
from smart_cs.persistence.postgres import MemoryCheckpointLocks,PostgresCheckpointStore,prepare_checkpoint_store
from smart_cs.persistence.conversations import ConversationRuntime
from smart_cs.repositories.runs import DatabaseRunRepository
from smart_cs.services.chat import ChatService
from smart_cs.tracing.otel_config import AgentMetrics


class CompoundTests(ApprovalFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp();self.orchestrator=TaskOrchestrator(self.service,None)
        self.goal=f'先解释退款政策，再核验本人订单 {self.order}，符合条件帮我申请退款100元，原因商品不合适。'

    def proposal(self,*,amount=True,details=True,kinds=None):
        slots={'order_id':self.order,'type':'refund','priority':'medium'}
        if amount:slots['requested_amount']='100.00'
        if details:slots.update(summary='退款申请',details='商品不合适')
        return {'tasks':kinds or ['policy_lookup','order_eligibility','ticket_create'],'slots':slots}

    def state(self,goal=None,proposal=None):
        return {'messages':[HumanMessage(content=goal or self.goal)],'session_id':'fixture-session','turn_id':uuid4().hex,
            'business_context_fingerprint':'same-context','intent_details':{'confidence':.99,'secondary':'compound'},
            'task_proposal':proposal or self.proposal(),'sub_results':{},'agent_steps':[]}

    async def initialized(self,state):
        token=principal_context.set(self.a)
        try:return {**state,**await self.orchestrator.initialize(state)}
        finally:principal_context.reset(token)

    async def test_router_accepts_bounded_tasks_but_rejects_identity_dependencies_and_invalid_types(self):
        from test_compliance_reviews import Model
        base={'primary_intent':'transaction','secondary_intent':'compound','confidence':.99,'suggested_agent':'ticket_handler',**self.proposal()}
        result=await IntentRouterAgent(Model(base)).classify(self.goal)
        self.assertEqual(result.tasks,['policy_lookup','order_eligibility','ticket_create'])
        security={**base,'primary_intent':'compliance','suggested_agent':'compliance_checker','tasks':[],'slots':{}}
        routed=await IntentRouterAgent(Model(security)).classify('如何保护验证码安全？')
        self.assertEqual(routed.suggested_agent,'compliance_checker');self.assertEqual(routed.confidence,.99)
        slot_only={**base,'tasks':[],'slots':{'requested_amount':'100.00'}}
        self.assertEqual((await IntentRouterAgent(Model(slot_only)).classify('100元')).slots['requested_amount'],'100.00')
        omitted={**base,'tasks':[],'slots':{}}
        self.assertEqual((await IntentRouterAgent(Model(omitted)).classify(self.goal)).confidence,0)
        for extra in [{'actor_id':'other'},{'approved':True},{'dependencies':['execute_now']}]:
            bad=copy.deepcopy(base);bad['slots'].update(extra)
            self.assertEqual((await IntentRouterAgent(Model(bad)).classify(self.goal)).confidence,0)
        bad={**base,'tasks':['ticket_create']*7}
        self.assertEqual((await IntentRouterAgent(Model(bad)).classify(self.goal)).confidence,0)

    async def test_server_compiles_order_dependencies_and_one_write_limit(self):
        state=await self.initialized(self.state(proposal=self.proposal(kinds=['ticket_create','policy_lookup'])))
        plan=TaskPlan.model_validate(state['task_plan'])
        self.assertEqual([t.kind for t in plan.tasks],['policy_lookup','order_eligibility','ticket_create'])
        self.assertEqual([t.depends_on for t in plan.tasks],[[],['t1'],['t2']])
        bad=await self.initialized(self.state(proposal=self.proposal(kinds=['ticket_create','ticket_transition'])))
        self.assertEqual(bad['task_plan']['status'],'blocked')

    async def test_slots_only_accept_human_literal_ids_amounts_and_versions(self):
        slots=sanitized_slots({'order_id':'fabricated','requested_amount':'500.00','expected_version':99},self.goal,[])
        self.assertIsNone(slots.order_id);self.assertIsNone(slots.requested_amount);self.assertIsNone(slots.expected_version)
        self.assertFalse(wants_write('只查询订单，不要创建工单'))
        self.assertTrue(wants_write('帮我登记退款申请，不要办理实际资金退款'))

    async def test_queries_and_low_confidence_never_dispatch_write(self):
        state=await self.initialized(self.state(goal=f'只查询订单 {self.order}，不要创建工单',proposal=self.proposal(kinds=['order_query','ticket_create'])))
        self.assertNotIn('ticket_create',[t['kind'] for t in state['task_plan']['tasks']])
        raw=self.state();raw['intent_details']['confidence']=.1
        self.assertEqual((await self.initialized(raw))['task_plan'],{})

    async def test_missing_slot_stops_before_business_model_and_public_summary_contains_no_free_text(self):
        state=await self.initialized(self.state(goal=f'帮我申请订单 {self.order} 的退款',proposal=self.proposal(amount=False,details=False,kinds=['ticket_create'])))
        state.update(await self.orchestrator.dispatch(state));self.assertEqual(self.orchestrator.route(state),'ticket_handler')
        # Read is allowed first; the write must wait for its own required slots.
        token=principal_context.set(self.a)
        try:state=await self.orchestrator.business(state)
        finally:principal_context.reset(token)
        state.update(await self.orchestrator.collect(state));state.update(await self.orchestrator.dispatch(state))
        self.assertEqual(state['task_plan']['status'],'needs_information')
        self.assertIn('requested_amount',state['task_plan']['missing_fields'])
        view=public_plan(state['task_plan']);self.assertNotIn('slots',view);self.assertNotIn('result',json.dumps(view))

    async def test_budget_and_unsatisfied_dependencies_stop_without_loop(self):
        state=await self.initialized(self.state());state['task_plan']['steps_used']=8
        stopped=await self.orchestrator.dispatch(state);self.assertEqual(stopped['task_plan']['status'],'blocked')
        state=await self.initialized(self.state());state['task_plan']['tasks'][0]['depends_on']=['absent']
        self.assertEqual((await self.orchestrator.dispatch(state))['task_plan']['status'],'blocked')

    async def test_no_policy_evidence_blocks_all_downstream_tasks(self):
        state=await self.initialized(self.state());state.update(await self.orchestrator.dispatch(state))
        state['knowledge_evidence']={'status':'clarify','protocol':'grounded-rag-v1','citations':[]}
        state['sub_results']={'knowledge_rag':'资料不足，请核实。'}
        stopped=await self.orchestrator.collect(state)
        self.assertEqual(stopped['task_plan']['status'],'blocked')
        self.assertTrue(all(t['status']=='blocked' for t in stopped['task_plan']['tasks']))

    async def test_denied_order_stops_before_draft(self):
        async with self.db.sessions.begin() as s:await s.execute(update(Order).where(Order.id==self.order).values(status='cancelled'))
        state=await self.initialized(self.state(proposal=self.proposal(kinds=['order_eligibility','ticket_create'])))
        state.update(await self.orchestrator.dispatch(state));token=principal_context.set(self.a)
        try:state=await self.orchestrator.business(state)
        finally:principal_context.reset(token)
        state.update(await self.orchestrator.collect(state))
        self.assertEqual(state['task_plan']['status'],'blocked');self.assertEqual(state['task_plan']['tasks'][-1]['reason_code'],'ORDER_NOT_ELIGIBLE')

    async def test_foreign_order_never_reaches_draft_or_discloses_foreign_facts(self):
        goal=f'帮我申请订单 {self.foreign} 退款100元，原因不合适';proposal=self.proposal(kinds=['order_eligibility','ticket_create']);proposal['slots']['order_id']=self.foreign
        state=await self.initialized(self.state(goal=goal,proposal=proposal));state.update(await self.orchestrator.dispatch(state));token=principal_context.set(self.a)
        try:state=await self.orchestrator.business(state)
        finally:principal_context.reset(token)
        state.update(await self.orchestrator.collect(state));self.assertEqual(state['task_plan']['status'],'blocked')
        self.assertIsNone(state['task_plan']['tasks'][0]['result']['data'])

    async def test_continuation_rechecks_business_and_changed_context_invalidates_policy_cache(self):
        state=await self.initialized(self.state(proposal=self.proposal(amount=False,details=False)))
        raw=state['task_plan'];raw['status']='needs_information'
        for task in raw['tasks'][:2]:task['status']='completed';task['result']={'text':'cached','knowledge_evidence':{'status':'answered'}}
        follow={**state,'task_plan':raw,'messages':[HumanMessage(content='退款100元，原因商品不合适')],'task_proposal':self.proposal()}
        continued=await self.initialized(follow)
        self.assertEqual(continued['task_plan']['tasks'][0]['status'],'completed')
        self.assertEqual(continued['task_plan']['tasks'][1]['status'],'pending')
        follow['business_context_fingerprint']='changed';changed=await self.initialized(follow)
        self.assertTrue(all(t['status']=='pending' for t in changed['task_plan']['tasks']))
        self.assertFalse(changed['knowledge_evidence'])

    async def test_cancel_or_explicit_query_does_not_continue_old_write(self):
        state=await self.initialized(self.state());state['task_plan']['status']='needs_information'
        follow={**state,'messages':[HumanMessage(content='算了，取消这个办理任务')],'task_proposal':{'tasks':[],'slots':{}}}
        cancelled=await self.initialized(follow);self.assertEqual(cancelled['task_plan']['status'],'blocked')
        follow.update(messages=[HumanMessage(content=f'这次只查询订单 {self.order}')],task_proposal={'tasks':['order_query'],'slots':{'order_id':self.order}})
        new=await self.initialized(follow);self.assertEqual([t['kind'] for t in new['task_plan']['tasks']],['order_query'])

    async def graph_flow(self,*,clarify=False,reopen=False,blocked=False,stale=False):
        if reopen:
            await prepare_checkpoint_store(self.db);store=await PostgresCheckpointStore(self.db).open()
        else:store=MemoryCheckpointLocks()
        knowledge_calls=[];executions=ActionExecutionService(self.db,clock=lambda:self.now)
        async def knowledge(agent,state):
            knowledge_calls.append(True)
            return {**state,'knowledge_evidence':{'status':'answered','protocol':'grounded-rag-v1','claims':[],
                'citations':[{'source':'explicit-test-fixture','url':'/fixture'}],'sources':['explicit-test-fixture']},
                'sub_results':{**state['sub_results'],'knowledge_rag':'测试资料范围内的政策说明。[1]'},
                'agent_steps':[*state['agent_steps'],'knowledge_rag'],'agent_models':{**state['agent_models'],'knowledge_rag':'fixture'}}
        initial=self.proposal(amount=not clarify,details=not clarify)
        goal=self.goal if not clarify else f'先解释退款政策，再核验本人订单 {self.order}，符合条件帮我申请退款。'
        def build(recovery=False):
            route=lambda p:json.dumps({'primary_intent':'transaction','secondary_intent':'compound','confidence':.99,'suggested_agent':'ticket_handler',**p})
            responses=([json.dumps(ALLOW_CONTENT),json.dumps(ALLOW_ACTION),json.dumps(ALLOW_CONTENT),json.dumps(ALLOW_CONTENT)] if clarify else [json.dumps(ALLOW_ACTION),json.dumps(BLOCK_CONTENT if blocked else ALLOW_CONTENT),json.dumps(ALLOW_CONTENT)])
            if recovery:responses=[json.dumps(ALLOW_ACTION),json.dumps(ALLOW_CONTENT),json.dumps(ALLOW_CONTENT)]
            planner=self.plan(summary='退款申请',details='商品不合适').model_dump_json()
            models={role:FakeListChatModel(responses=values) for role,values in {'intent_router':[route(self.proposal() if recovery else initial),route(self.proposal())],
                'ticket_handler':[planner],'knowledge_rag':['unused'],'compliance_checker':responses}.items()}
            if reopen:checkpointer=store.saver
            else:
                from langgraph.checkpoint.memory import MemorySaver
                checkpointer=MemorySaver()
            workflow=create_supervisor_graph(agent_llms=models,tool_service=self.service,approval_service=self.approvals,execution_service=executions,checkpointer=checkpointer)
            repository=DatabaseRunRepository(self.db)
            return ChatService(workflow,None,repository,AgentMetrics(),ConversationRuntime(self.db,store,workflow,repository))
        service=build()
        async def run(message,identifier=None,resume=False):
            req=ChatRequest(message=message,session_id=identifier);p=service.prepare(req,self.a)
            async with AsyncExitStack() as resources:
                await service.conversations.prepare(req,p,resources,resume=resume)
                with patch('smart_cs.services.actions.ActionPlanService',return_value=self.service),patch('smart_cs.services.execution.ActionExecutionService',return_value=executions):
                    events=[e async for e in service.events(req,p)]
            self.assertEqual(events[-1]['type'],'complete',events[-1]);return p,events[-1]
        try:
            with patch('smart_cs.agents.knowledge_rag.KnowledgeRAGAgent.process',new=knowledge),patch('smart_cs.retrieval.dense.DenseRetriever.revalidate_citations',new=AsyncMock()),patch('smart_cs.services.approvals.ApprovalService',return_value=self.approvals):
                p,first=await run(goal)
                if blocked:
                    self.assertFalse(first['compliance_passed']);self.assertEqual(first['task_plan']['status'],'blocked');self.assertFalse(first['action_request']);return
                if clarify:
                    self.assertEqual(first['task_plan']['status'],'needs_information');self.assertIn('requested_amount',first['task_plan']['missing_fields'])
                    self.assertFalse(first['action_request']);self.assertTrue((await service.conversations.detail(p.session_id,self.a))['can_continue'])
                    if reopen:
                        await store.close();store=await PostgresCheckpointStore(self.db).open();service=build(recovery=True)
                    p,draft=await run('退款100元，原因商品不合适。',p.session_id)
                else:draft=first
                self.assertTrue(draft['compliance_passed']);self.assertEqual(draft['task_plan']['status'],'waiting_approval')
                self.assertEqual(len(knowledge_calls),1)
                self.assertEqual(draft['task_plan']['completed_task_ids'],['t1','t2'])
                view=draft['action_request'];await self.approvals.decide(self.a,view['action_id'],self.decision(view))
                if stale:
                    async with self.db.sessions.begin() as s:await s.execute(update(Ticket).where(Ticket.id==self.ticket).values(version=3))
                    before=await self.facts()
                    with self.assertRaisesRegex(ApplicationError,'ACTION_STALE'):
                        await executions.execute(self.a,view['action_id'],view['parameter_hash'])
                    self.assertEqual(before,await self.facts());return
                _,completed=await run('resume',p.session_id,True)
                self.assertTrue(completed['compliance_passed']);self.assertEqual(completed['task_plan']['status'],'completed')
                self.assertEqual(completed['task_plan']['completed_task_ids'],['t1','t2','t3'])
                self.assertFalse(completed['citations']);self.assertTrue(completed['action_execution'])
                self.assertEqual(len(knowledge_calls),1)
                self.assertEqual(await executions.get(self.a,view['action_id']),completed['action_execution'])
                with self.assertRaisesRegex(ApplicationError,'TURN_ALREADY_COMPLETED'):await run('resume',p.session_id,True)
        finally:await store.close()

    async def test_full_four_agent_dependency_and_approval_execution_flow(self):await self.graph_flow()
    async def test_full_graph_clarifies_and_reuses_completed_policy(self):await self.graph_flow(clarify=True)
    async def test_blocked_content_cannot_publish_plan_or_enter_approval(self):await self.graph_flow(blocked=True)
    async def test_direct_execution_rechecks_whole_task_context_after_approval(self):await self.graph_flow(stale=True)

    @unittest.skipUnless(os.getenv('AUTH_VERIFY_SCHEMA'),'Requires PostgreSQL saver reopen')
    async def test_missing_slots_survive_actual_postgres_saver_reopen(self):await self.graph_flow(clarify=True,reopen=True)


if __name__=='__main__':unittest.main()
