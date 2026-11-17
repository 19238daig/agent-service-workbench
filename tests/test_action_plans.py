"""Same tests run on isolated SQLite and on a restricted PostgreSQL schema."""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
import httpx
from sqlalchemy import select,update,func
from pydantic import ValidationError
from langchain_core.messages import HumanMessage
from smart_cs.db.database import Database
from smart_cs.db.models import User,Order,Ticket,TicketEvent
from smart_cs.repositories.orders import OrderRepository
from smart_cs.repositories.tickets import PostgresTicketRepository
from smart_cs.services.orders import OrderService
from smart_cs.services.tickets import TicketService
from smart_cs.services.actions import ActionPlanService
from smart_cs.schemas.actions import ActionPlan,decode_plan
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.access import principal_context
from smart_cs.agents.action_planner import plan_turn
from smart_cs.tools.business import tool_catalog,READ_TOOLS

NOW=datetime(2026,11,14,12,tzinfo=timezone.utc)


class Model:
    def __init__(self,response):self.response=response
    async def ainvoke(self,*args):return SimpleNamespace(content=json.dumps(self.response) if isinstance(self.response,dict) else self.response)


class ActionPlanFixture:
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.url='sqlite+aiosqlite:///'+(Path(cls.temp.name)/'actions.db').as_posix();cls.schema=None
        if os.getenv('AUTH_VERIFY_SCHEMA'):cls.url=os.environ['DATABASE_URL'];cls.schema=os.environ['AUTH_VERIFY_SCHEMA']
        async def initialize():
            db=Database(cls.url,schema=cls.schema)
            try:await db.initialize(create_schema=True)
            finally:await db.close()
        asyncio.run(initialize())

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    async def asyncSetUp(self):
        self.db=Database(self.url,schema=self.schema);prefix=uuid4().hex[:10]
        self.a=IdentityContext(subject_id=prefix+'-a',authenticated=True,mode='authenticated_session')
        self.b=self.a.model_copy(update={'subject_id':prefix+'-b'})
        self.staff=self.a.model_copy(update={'subject_id':prefix+'-s','role':'support'})
        self.admin=self.a.model_copy(update={'subject_id':prefix+'-admin','role':'admin'})
        self.order=prefix+'-order-a';self.foreign=prefix+'-order-b';self.ticket=prefix+'-ticket'
        async with self.db.sessions.begin() as s:
            s.add_all([User(id=i.subject_id,display_name='Fixture',role=i.role) for i in [self.a,self.b,self.staff,self.admin]])
            await s.flush()
            for identifier,owner in [(self.order,self.a),(self.foreign,self.b)]:
                s.add(Order(id=identifier,user_id=owner.subject_id,product_name='Fixture',paid_amount=Decimal('500.00'),status='pending_shipment',
                    paid_at=NOW-timedelta(days=1),refunded_amount=Decimal('100.01'),refund_reserved_amount=Decimal('49.99'),
                    return_policy='standard_7d',return_condition='intact',fact_version=3))
            await s.flush()
            s.add(Ticket(id=self.ticket,user_id=self.a.subject_id,order_id=self.order,type='refund',priority='medium',status='processing',summary='Fixture',details='Fixture',version=2,assignee_id=self.staff.subject_id))
        self.service=ActionPlanService(OrderService(OrderRepository(self.db),clock=lambda:NOW),TicketService(PostgresTicketRepository(self.db)),clock=lambda:NOW)

    async def asyncTearDown(self):await self.db.close()

    def plan(self,name='ticket_create',**arguments):
        if name=='ticket_create':arguments={'type':'refund','order_id':self.order,'requested_amount':'100.00','summary':'申请登记','details':'测试申请原因',**arguments}
        return ActionPlan(action={'name':name,'arguments':arguments})

    async def facts(self):
        async with self.db.sessions() as s:
            return [(await s.execute(select(*columns))).all() for columns in [
                [Order.id,Order.status,Order.paid_amount,Order.refunded_amount,Order.refund_reserved_amount,Order.fact_version],
                [Ticket.id,Ticket.status,Ticket.version,Ticket.assignee_id],[TicketEvent.id,TicketEvent.event_type]]]


class ActionPlanTests(ActionPlanFixture,unittest.IsolatedAsyncioTestCase):
    async def test_valid_refund_is_only_a_bound_draft_and_same_turn_id_is_stable(self):
        before=await self.facts();plan=self.plan()
        first=await self.service.validate(self.a,plan,binding='server-turn')
        again=await self.service.validate(self.a,plan,binding='server-turn')
        self.assertEqual(first.status,'draft_validated');self.assertEqual(first.action_id,again.action_id)
        self.assertEqual(first.parameter_hash,again.parameter_hash);self.assertEqual(first.actor_id,self.a.subject_id)
        self.assertEqual(first.fact_versions,{f'order:{self.order}':3});self.assertFalse(first.execution_enabled);self.assertFalse(first.executed)
        self.assertEqual(before,await self.facts())

    async def test_parameters_and_actor_are_hash_bound_and_new_requests_do_not_authorize_replay(self):
        first=await self.service.validate(self.a,self.plan(),binding='turn')
        changed=await self.service.validate(self.a,self.plan(requested_amount='99.00'),binding='turn')
        other=await self.service.validate(self.b,self.plan(),binding='turn')
        self.assertNotEqual(first.parameter_hash,changed.parameter_hash);self.assertNotEqual(first.action_id,changed.action_id)
        self.assertNotEqual(first.parameter_hash,other.parameter_hash);self.assertEqual(other.status,'rejected')

    async def test_missing_refund_order_amount_and_description_require_slots(self):
        before=await self.facts()
        result=await self.service.validate(self.a,self.plan(order_id=None,requested_amount=None,summary=None,details=None))
        self.assertEqual(result.status,'needs_information')
        self.assertEqual(set(result.missing_fields),{'order_id','requested_amount','summary','details'})
        self.assertEqual(before,await self.facts())

    async def test_foreign_and_unknown_orders_do_not_disclose_facts(self):
        for identity,order in [(self.a,self.foreign),(self.a,'missing'),(self.admin,self.order)]:
            result,data=await self.service.assess(identity,self.plan(order_id=order))
            self.assertEqual(result.status,'rejected');self.assertEqual(result.reason_codes,['ORDER_NOT_FOUND']);self.assertIsNone(data)

    async def test_amount_and_business_state_are_checked_without_writes(self):
        before=await self.facts()
        for amount,reason in [('350.01','REQUEST_EXCEEDS_MAXIMUM'),('0.00','INVALID_AMOUNT')]:
            result=await self.service.validate(self.a,self.plan(requested_amount=amount))
            self.assertEqual(result.status,'rejected');self.assertIn(reason,result.reason_codes)
        self.assertEqual(before,await self.facts())
        async with self.db.sessions.begin() as s:await s.execute(update(Order).where(Order.id==self.order).values(status='cancelled',fact_version=4))
        result=await self.service.validate(self.a,self.plan())
        self.assertEqual(result.status,'rejected');self.assertIn('ORDER_CANCELLED',result.reason_codes)

    async def test_non_refund_amount_and_unsupported_business_are_rejected(self):
        result=await self.service.validate(self.a,self.plan(type='general'))
        self.assertIn('AMOUNT_NOT_APPLICABLE',result.reason_codes)
        with self.assertRaises(ValidationError):self.plan(type='account_open')

    async def test_transitions_require_assignment_allowed_target_and_expected_version(self):
        before=await self.facts()
        valid=self.plan('ticket_transition',ticket_id=self.ticket,target_status='resolved',expected_version=2,reason='已核实')
        self.assertEqual((await self.service.validate(self.staff,valid)).status,'draft_validated')
        for identity,params,code in [(self.admin,{},'TICKET_NOT_FOUND'),(self.a,{},'TICKET_TRANSITION_NOT_ALLOWED'),
            (self.staff,{'expected_version':1},'TICKET_VERSION_CONFLICT'),(self.staff,{'target_status':'created'},'TICKET_TRANSITION_NOT_ALLOWED')]:
            plan=self.plan('ticket_transition',**{**valid.action.arguments.model_dump(),**params})
            self.assertIn(code,(await self.service.validate(identity,plan)).reason_codes)
        self.assertEqual(before,await self.facts())

    async def test_read_tools_share_acl_and_do_not_create(self):
        before=await self.facts()
        rows=await self.service.call_read(self.a,'order_query',{'order_id':self.order})
        self.assertEqual(rows[0]['paid_amount'],'500.00');self.assertEqual(rows[0]['user_id'],self.a.subject_id)
        eligibility=await self.service.call_read(self.a,'order_eligibility',{'order_id':self.order})
        self.assertEqual(eligibility['maximum_refund_amount'],'350.00')
        ticket=await self.service.call_read(self.staff,'ticket_query',{'ticket_id':self.ticket});self.assertEqual(ticket['version'],2)
        with self.assertRaisesRegex(ApplicationError,'TICKET_NOT_FOUND'):await self.service.call_read(self.b,'ticket_query',{'ticket_id':self.ticket})
        self.assertEqual(await self.service.call_read(self.b,'order_query',{'order_id':self.order}),[])
        self.assertEqual(before,await self.facts())

    async def test_every_write_tool_and_unknown_tool_is_denied_by_dispatch(self):
        before=await self.facts()
        for name in ['ticket_create','ticket_transition','refund_payment','unknown']:
            with self.assertRaisesRegex(ApplicationError,'TOOL_FORBIDDEN'):await self.service.call_read(self.a,name,{'confirm':True})
        self.assertEqual(before,await self.facts())
        catalog=tool_catalog();self.assertEqual(len(catalog),7)
        self.assertTrue(all(t['execution_enabled']==(t['name'] in READ_TOOLS) for t in catalog))

    async def test_schema_rejects_identity_approval_execution_money_facts_and_wrong_types(self):
        for extra in ['user_id','actor_id','role','action_id','approved','confirm_registration','paid_amount','maximum_refund_amount']:
            with self.assertRaises(ValidationError):self.plan(**{extra:'forged'})
        for amount in [100,True,'NaN','-1.00','1.001','1e2']:
            with self.assertRaises(ValidationError):self.plan(requested_amount=amount)
        for limit in [True,'5',101,-1]:
            with self.assertRaises(ValidationError):self.plan('order_query',limit=limit)
        with self.assertRaises(ValueError):decode_plan('{"action":{},"action":{}}')

    async def test_anonymous_identity_is_never_accepted_as_a_tool_principal(self):
        with self.assertRaisesRegex(ApplicationError,'UNAUTHENTICATED'):
            await self.service.validate(self.a.model_copy(update={'authenticated':False}),self.plan())

    async def invoke_agent(self,proposal,query,secondary='ticket_create',history=None):
        token=principal_context.set(self.a)
        try:return await plan_turn(Model(proposal),self.service,{'turn_id':'server-turn','intent_details':{'secondary':secondary}},query,history or [])
        finally:principal_context.reset(token)

    async def test_agent_returns_real_read_results_and_bound_draft_without_writer(self):
        before=await self.facts()
        plan,evidence,text=await self.invoke_agent(self.plan('order_query',order_id=self.order).model_dump(),f'查询 {self.order}，不要创建工单','order_query')
        self.assertTrue(plan['read_only']);self.assertTrue(plan['executed']);self.assertIn('500.00',text)
        plan,evidence,text=await self.invoke_agent(self.plan().model_dump(),f'为 {self.order} 申请退款100元')
        self.assertEqual(plan['status'],'draft_validated');self.assertFalse(plan['executed']);self.assertIn('尚未创建',text)
        self.assertEqual(before,await self.facts())

    async def test_agent_hallucinated_resource_amount_and_query_to_write_are_rejected(self):
        before=await self.facts()
        for query,secondary,reason in [('给我申请退款100元','ticket_create','UNSUPPORTED_MODEL_RESOURCE'),
            (f'申请 {self.order} 退款','ticket_create','UNSUPPORTED_MODEL_AMOUNT'),
            (f'查询 {self.order}，金额100元，不创建工单','order_query','READ_INTENT_WRITE_PROPOSAL')]:
            plan,evidence,text=await self.invoke_agent(self.plan().model_dump(),query,secondary)
            self.assertEqual(evidence['reason_code'],reason);self.assertFalse(plan)
        self.assertEqual(before,await self.facts())

    async def test_agent_cannot_accept_model_identity_or_approval_in_schema(self):
        for proposal in [{'action':{'name':'ticket_create','arguments':{'type':'refund','user_id':self.b.subject_id}}},'not-json']:
            plan,evidence,text=await self.invoke_agent(proposal,'申请退款')
            self.assertEqual(evidence['reason_code'],'INVALID_ACTION_PLAN');self.assertFalse(plan)

    async def test_api_validation_and_dispatch_are_authenticated_and_side_effect_free(self):
        from smart_cs.api import main
        from smart_cs.api.dependencies import current_identity
        original=dict(main.app.dependency_overrides);before=await self.facts()
        try:
            with patch.object(main,'database',self.db),patch.dict(os.environ,{'APP_MODE':'local_demo'}):
                main.app.dependency_overrides[current_identity]=lambda:self.a
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://testserver') as client:
                    response=await client.post('/api/actions/validate',json=self.plan().model_dump())
                    self.assertEqual(response.status_code,200);self.assertEqual(response.json()['status'],'draft_validated')
                    self.assertEqual(response.headers['cache-control'],'no-store')
                    self.assertFalse(response.json()['executed'])
                    for extra in [{'user_id':self.b.subject_id},{'action_id':'forged'},{'execute':True},{'approved':True}]:
                        self.assertEqual((await client.post('/api/actions/validate',json={**self.plan().model_dump(),**extra})).status_code,422)
                    for name in ['ticket_create','ticket_transition']:
                        self.assertEqual((await client.post('/api/tools/call',json={'name':name,'arguments':self.plan().action.arguments.model_dump()})).status_code,403)
                    self.assertEqual((await client.post('/api/tools/call',json={'name':'ticket_query','arguments':{'ticket_id':self.ticket}})).status_code,200)
                    main.app.dependency_overrides.pop(current_identity)
                    self.assertEqual((await client.post('/api/actions/validate',json=self.plan().model_dump())).status_code,401)
        finally:main.app.dependency_overrides.clear();main.app.dependency_overrides.update(original)
        self.assertEqual(before,await self.facts())


if __name__=='__main__':unittest.main()
