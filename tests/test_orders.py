"""Order rules, Decimal arithmetic, ownership, typed tools and actual agent wiring."""
import asyncio
import json
import os
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from sqlalchemy import select,update,func
from sqlalchemy.exc import IntegrityError
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from smart_cs.db.database import Database
from smart_cs.db.models import Base,Order,User,AuthCredential,Ticket
from smart_cs.order_fixtures import load_order_scenarios,seed_order_scenarios
from smart_cs.repositories.orders import OrderRepository
from smart_cs.services.orders import OrderService,evaluate_refund
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.tools.orders import call_order_tool
from smart_cs.agents.supervisor import create_supervisor_graph
import smart_cs.api.main as main
import test_auth as auth_support

NOW=datetime(2026,11,14,tzinfo=timezone.utc)


def identity(subject='a',role='customer'):
    return IdentityContext(subject_id=subject,role=role,authenticated=True,mode='authenticated_session')


class PolicyTests(unittest.TestCase):
    def setUp(self):self.manifest,self.rows=load_order_scenarios();self.facts=self.rows[1].facts

    def test_all_fixed_scenarios_match_manifest_and_expected_outcomes(self):
        self.assertEqual(len(self.rows),14)
        for row in self.rows:
            with self.subTest(row.case):
                result=evaluate_refund(row.facts,NOW)
                self.assertEqual(result.decision,row.expected_decision)
                self.assertEqual(result.maximum_refund_amount,row.expected_maximum)

    def test_exact_boundary_before_and_after_and_utc_offset(self):
        deadline=self.facts.delivered_at+timedelta(days=7)
        self.assertTrue(evaluate_refund(self.facts,deadline).can_apply)
        self.assertFalse(evaluate_refund(self.facts,deadline+timedelta(microseconds=1)).can_apply)
        self.assertTrue(evaluate_refund(self.facts,deadline-timedelta(microseconds=1)).can_apply)
        self.assertTrue(evaluate_refund(self.facts,deadline.astimezone(timezone(timedelta(hours=8)))).can_apply)

    def test_partial_refund_and_reservation_use_decimal_not_float(self):
        result=evaluate_refund(self.rows[5].facts,NOW)
        self.assertEqual(result.maximum_refund_amount,'350.00')
        for paid,refunded,reserved,expected in [('0.30','0.10','0.00','0.20'),('500.00','100.01','49.99','350.00')]:
            facts=self.facts.model_copy(update={'paid_amount':paid,'refunded_amount':refunded,'refund_reserved_amount':reserved})
            self.assertEqual(evaluate_refund(facts,NOW).remaining_paid_amount,expected)

    def test_unknown_historical_facts_are_not_zero_or_inferred_delivery(self):
        result=evaluate_refund(self.rows[8].facts,NOW)
        self.assertEqual(result.decision,'needs_information');self.assertIsNone(result.remaining_paid_amount)
        self.assertEqual(result.maximum_refund_amount,'0.00')
        self.assertIn('delivered_at',result.missing_facts);self.assertIn('refunded_amount',result.missing_facts)

    def test_future_and_inconsistent_times_never_allow_application(self):
        for update_values in [{'paid_at':NOW+timedelta(days=1)},{'delivered_at':NOW+timedelta(days=1)},
                              {'shipped_at':self.facts.paid_at-timedelta(seconds=1)},
                              {'status':'pending_shipment'}]:
            result=evaluate_refund(self.facts.model_copy(update=update_values),NOW)
            self.assertFalse(result.can_apply);self.assertEqual(result.reason_codes,['FACTS_INCONSISTENT'])

    def test_requested_amount_is_compared_with_database_cap(self):
        facts=self.rows[5].facts
        self.assertTrue(evaluate_refund(facts,NOW,'350.00').can_apply)
        result=evaluate_refund(facts,NOW,'350.01')
        self.assertFalse(result.can_apply);self.assertEqual(result.reason_codes,['REQUEST_EXCEEDS_MAXIMUM'])
        self.assertEqual(result.maximum_refund_amount,'350.00')


class OrderRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.url='sqlite+aiosqlite:///'+str(Path(self.temp.name)/'orders.db').replace('\\','/')
        self.schema=os.getenv('AUTH_VERIFY_SCHEMA')
        if self.schema:self.url=os.environ['DATABASE_URL']
        self.db=Database(self.url,schema=self.schema)
        if self.schema:
            self.assertEqual(self.db.engine.get_execution_options().get('schema_translate_map'),{None:self.schema})
            async with self.db.engine.begin() as connection:await connection.run_sync(Base.metadata.drop_all)
        await self.db.initialize(create_schema=True)
        async with self.db.sessions.begin() as session:
            session.add_all([User(id='a',display_name='A',role='customer'),User(id='b',display_name='B',role='customer')]);await session.flush()
            session.add_all([AuthCredential(user_id='a',login_name='customer-a',password_hash='not-a-login-hash'),
                             AuthCredential(user_id='b',login_name='customer-b',password_hash='not-a-login-hash')])
        await seed_order_scenarios(self.db)
        self.service=OrderService(OrderRepository(self.db),clock=lambda:NOW)

    async def asyncTearDown(self):await self.db.close();self.temp.cleanup()

    async def test_owner_filter_pagination_and_direct_query_beyond_first_100(self):
        page=await self.service.list(identity(),limit=5,offset=5)
        self.assertEqual(page.total,12);self.assertEqual(len(page.orders),5);self.assertTrue(page.has_more)
        with self.assertRaises(ApplicationError) as foreign:await self.service.get(identity(),'ORDER-B-001')
        with self.assertRaises(ApplicationError) as missing:await self.service.get(identity(),'MISSING')
        self.assertEqual(foreign.exception.code,missing.exception.code)
        async with self.db.sessions.begin() as session:
            for i in range(105):session.add(Order(id=f'Z-{i:03d}',user_id='a',product_name='合成订单',paid_amount=Decimal('1'),currency='CNY',status='pending_shipment',paid_at=NOW))
        result=await call_order_tool(self.service,identity(),'order_query',{'order_id':'Z-104','user_id':'b'})
        self.assertEqual(result[0]['order_id'],'Z-104');self.assertEqual(result[0]['user_id'],'a')
        self.assertEqual(await call_order_tool(self.service,identity(),'order_query',{'order_id':'ORDER-B-001','user_id':'b'}),[])

    async def test_read_checks_have_no_business_writes_and_survive_new_engine(self):
        before=(await self.db.summary())['counts']
        result=await self.service.eligibility(identity(),'ORDER-A-006','350.00');self.assertTrue(result.can_apply)
        self.assertEqual((await self.db.summary())['counts'],before)
        await self.db.close();self.db=Database(self.url,schema=self.schema)
        result=await OrderService(OrderRepository(self.db),clock=lambda:NOW).eligibility(identity(),'ORDER-A-006')
        self.assertEqual(result.maximum_refund_amount,'350.00')

    async def test_reseed_preserves_existing_edited_facts_and_detects_owner_collision(self):
        async with self.db.sessions.begin() as session:
            await session.execute(update(Order).where(Order.id=='ORDER-A-006').values(refunded_amount=Decimal('120.01'),fact_version=2))
        seeded=await seed_order_scenarios(self.db);self.assertEqual(seeded['inserted'],0);self.assertEqual(seeded['retained'],14)
        row=await self.service.get(identity(),'ORDER-A-006');self.assertEqual(row.refunded_amount,'120.01');self.assertEqual(row.fact_version,2)
        async with self.db.sessions.begin() as session:await session.execute(update(Order).where(Order.id=='ORDER-A-001').values(user_id='b'))
        with self.assertRaises(ValueError):await seed_order_scenarios(self.db)
        self.assertEqual((await self.service.get(identity('b'),'ORDER-A-001')).user_id,'b')

    async def test_service_rejects_nonfinite_float_negative_zero_and_oversized_money(self):
        for value in ['NaN','Infinity','-1.00','0.00','1.001','10000000000.00',3.0,True]:
            with self.subTest(value=value),self.assertRaises(ApplicationError):await self.service.eligibility(identity(),'ORDER-A-002',value)
        for arguments in [{'order_id':'ORDER-A-002','paid_amount':'999.00'},{'order_id':'ORDER-A-002','user_id':'b'}, {'order_id':'ORDER-A-002','requested_amount':5}]:
            with self.assertRaises(ApplicationError):await call_order_tool(self.service,identity(),'order_eligibility',arguments)

    async def test_database_refund_and_time_constraints_and_transaction_rollback(self):
        data=load_order_scenarios()[1][1].facts.model_dump(exclude={'order_id','user_id'})
        for name in ['paid_amount','refunded_amount','refund_reserved_amount']:data[name]=Decimal(data[name])
        changes=[{'refunded_amount':Decimal('-0.01')},{'refund_reserved_amount':Decimal('-0.01')},
                 {'refunded_amount':Decimal('400.00')},{'refunded_amount':Decimal('399.98'),'refund_reserved_amount':Decimal('0.02')},
                 {'shipped_at':NOW-timedelta(days=20)},{'shipped_at':None},{'delivered_at':NOW-timedelta(days=6)},
                 {'return_policy':'invented'},{'return_condition':'invented'},{'fact_version':0}]
        if self.db.engine.dialect.name=='postgresql':changes.extend([{'refunded_amount':Decimal('NaN')},{'refund_reserved_amount':Decimal('NaN')}])
        for i,change in enumerate(changes):
            with self.subTest(change=list(change)),self.assertRaises(IntegrityError):
                async with self.db.sessions.begin() as session:session.add(Order(id=f'invalid-{i}',user_id='a',**(data|change)));await session.flush()
        with self.assertRaises(IntegrityError):
            async with self.db.sessions.begin() as session:
                session.add(Order(id='rollback',user_id='a',**data));await session.flush()
                session.add(Order(id='rollback-bad',user_id='a',**(data|{'refunded_amount':Decimal('-1')})));await session.flush()
        self.assertIsNone(await OrderRepository(self.db).get('rollback','a'))


class OrderApiTests(unittest.TestCase):
    db=auth_support.AuthTests.db
    execute=auth_support.AuthTests.execute
    login=auth_support.AuthTests.login

    def setUp(self):
        auth_support.AuthTests.setUp(self)
        async def seed(db):
            async with db.sessions.begin() as session:
                session.add_all([User(id='fixture-a',display_name='A',role='customer'),User(id='fixture-b',display_name='B',role='customer')]);await session.flush()
                session.add_all([AuthCredential(user_id='fixture-a',login_name='customer-a',password_hash=auth_support.PASSWORD_HASH),
                                 AuthCredential(user_id='fixture-b',login_name='customer-b',password_hash=auth_support.PASSWORD_HASH)])
            await seed_order_scenarios(db)
        self.execute(seed)
        value=patch.object(main,'order_service',side_effect=lambda:OrderService(OrderRepository(main.database),clock=lambda:NOW))
        value.start();self.addCleanup(value.stop)
        self.a=self.login('customer-a');self.b=self.login('customer-b')

    def test_actual_auth_ownership_and_all_three_read_interfaces(self):
        for path in ['/api/orders/ORDER-A-006','/api/orders/ORDER-A-006/refund-eligibility']:
            self.assertEqual(self.client.get(path).status_code,401)
            self.assertEqual(self.client.get(path,headers=self.a).status_code,200)
            self.assertEqual(self.client.get(path+'?user_id=fixture-a',headers=self.b).status_code,404)
        page=self.client.get('/api/orders?limit=5&offset=5&user_id=fixture-b',headers=self.a).json()
        self.assertEqual(page['total'],12);self.assertEqual(len(page['orders']),5)
        self.assertTrue(all(row['user_id']=='fixture-a' for row in page['orders']))
        result=self.client.get('/api/orders/ORDER-A-006/refund-eligibility?requested_amount=350.00',headers=self.a).json()
        self.assertEqual(result['maximum_refund_amount'],'350.00');self.assertTrue(result['can_apply'])
        result=self.client.post('/api/tools/call',headers=self.a,json={'name':'order_eligibility','arguments':{'order_id':'ORDER-A-006'}}).json()
        self.assertEqual(result['result']['maximum_refund_amount'],'350.00')
        self.assertEqual(self.client.post('/api/tools/call',headers=self.b,json={'name':'order_eligibility','arguments':{'order_id':'ORDER-A-006'}}).status_code,404)

    def test_tool_and_http_amount_validation_cannot_inject_facts_or_approval(self):
        for amount in ['NaN','-1.00','0.00','1.001','10000000000.00']:
            self.assertEqual(self.client.get('/api/orders/ORDER-A-006/refund-eligibility',headers=self.a,params={'requested_amount':amount}).status_code,422)
        for args in [{'order_id':'ORDER-A-006','paid_amount':'999.00'},{'order_id':'ORDER-A-006','decision':'allowed'},
                     {'order_id':'ORDER-A-006','requested_amount':100},{'order_id':'ORDER-A-006','user_id':'fixture-b'}]:
            self.assertEqual(self.client.post('/api/tools/call',headers=self.a,json={'name':'order_eligibility','arguments':args}).status_code,422)
        self.assertEqual(self.client.post('/api/tools/call',headers=self.a,json={'name':'ticket_create','arguments':{}}).status_code,403)
        result=self.client.get('/api/orders/ORDER-A-006/refund-eligibility?requested_amount=350.01',headers=self.a).json()
        self.assertFalse(result['can_apply']);self.assertEqual(result['reason_codes'],['REQUEST_EXCEEDS_MAXIMUM'])
        expired=self.client.get('/api/orders/ORDER-A-003/refund-eligibility',headers=self.a,
            params={'as_of':'2026-11-06T00:00:00Z','paid_amount':'999999.00','user_id':'fixture-b'}).json()
        self.assertEqual(expired['reason_codes'],['WINDOW_EXPIRED'])

    def test_agent_discards_fabricated_money_and_owner_while_preserving_read_only_semantics(self):
        async def counts(db):
            async with db.sessions() as session:return await session.scalar(select(func.count()).select_from(Ticket))
        before=self.execute(counts)
        for action,order_id in [('refund_eligibility','ORDER-A-006'),('order_query','ORDER-A-006'),('refund_eligibility','ORDER-B-001')]:
            fake=FakeListChatModel(responses=['{"primary_intent":"transaction","confidence":0.99,"suggested_agent":"ticket_handler"}',
                json.dumps({'action':action,'order_id':order_id,'user_id':'fixture-b','paid_amount':'999999.00','maximum_refund_amount':'999999.00'}),
                '{"passed":true,"risk_level":"low"}'])
            graph=create_supervisor_graph(llm=fake,order_service=OrderService(OrderRepository(main.database),clock=lambda:NOW))
            with patch.object(main,'graph',graph):
                response=self.client.post('/api/chat',headers=self.a,json={'message':'查询并核验此订单','user_id':'fixture-b'})
            self.assertEqual(response.status_code,200,response.text);answer=response.json()['response']
            self.assertNotIn('999999',answer)
            if order_id=='ORDER-B-001':self.assertIn('未找到',answer);self.assertNotIn('120.00',answer)
            else:
                self.assertIn('500.00',answer)
                if action=='refund_eligibility':self.assertIn('350.00',answer)
            self.assertEqual(self.execute(counts),before)
        # A later model cannot turn a recognized read intent into ticket creation.
        fake=FakeListChatModel(responses=['{"primary_intent":"transaction","secondary_intent":"refund_eligibility","confidence":0.99,"suggested_agent":"ticket_handler"}',
            '{"action":"create","ticket_type":"refund","summary":"unwanted write","order_id":"ORDER-A-006"}',
            '{"passed":true,"risk_level":"low"}'])
        graph=create_supervisor_graph(llm=fake,order_service=OrderService(OrderRepository(main.database),clock=lambda:NOW))
        with patch.object(main,'graph',graph):
            response=self.client.post('/api/chat',headers=self.a,json={'message':'只核验条件，不建单'})
        self.assertEqual(response.status_code,200)
        self.assertIn('不会自动创建',response.json()['response'])
        self.assertEqual(self.execute(counts),before)


if __name__=='__main__':unittest.main()
