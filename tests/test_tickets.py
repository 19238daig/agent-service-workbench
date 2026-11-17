"""Transactional ticket state machine, explicit assignment and Agent read boundary."""
import asyncio
from datetime import datetime,timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from sqlalchemy import select,update,func
from sqlalchemy.exc import IntegrityError
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from smart_cs.db.database import Database
from smart_cs.db.models import Base,User,AuthCredential,Order,Ticket,TicketEvent
from smart_cs.repositories.tickets import PostgresTicketRepository
from smart_cs.services.tickets import TicketService,AgentTicketReader,TRANSITIONS
from smart_cs.schemas.tickets import TicketRegistration,TicketTransition,TicketAssignment
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.order_fixtures import seed_order_scenarios
from smart_cs.ticket_fixtures import seed_ticket_scenarios
from smart_cs.agents.supervisor import create_supervisor_graph
import smart_cs.api.main as main
import test_auth as auth_support


def identity(subject='a',role='customer'):
    return IdentityContext(subject_id=subject,role=role,authenticated=True,mode='authenticated_session')


def registration(**changes):
    return TicketRegistration.model_validate({'type':'general','summary':'合成服务申请','confirm_registration':True,**changes})


def transition(target,version):return TicketTransition(target_status=target,expected_version=version,reason='合成流转验证')
def assignment(staff,version):return TicketAssignment(assignee_id=staff,expected_version=version,reason='用户明确选择处理人员')


class TicketContractTests(unittest.TestCase):
    def test_registration_confirmation_and_actor_are_not_coerced_or_client_selected(self):
        for changes in [{'confirm_registration':False},{'confirm_registration':1},{'confirm_registration':'true'},
                        {'summary':'   '},{'user_id':'other'},{'actor_role':'admin'},{'source':'agent'}]:
            with self.subTest(changes=list(changes)),self.assertRaises(ValidationError):registration(**changes)
        for value in [True,0,-1,'1']:
            with self.assertRaises(ValidationError):TicketTransition(target_status='processing',expected_version=value,reason='verify')

    def test_closed_is_terminal_and_no_approval_or_payment_status_exists(self):
        self.assertEqual(TRANSITIONS['closed'],frozenset())
        self.assertNotIn('resolved',TRANSITIONS['created'])
        for status in ['refund_approved','paid','reopened']:
            with self.assertRaises(ValidationError):transition(status,1)


class TicketServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.url='sqlite+aiosqlite:///'+str(Path(self.temp.name)/'tickets.db').replace('\\','/')
        self.schema=os.getenv('AUTH_VERIFY_SCHEMA')
        if self.schema:self.url=os.environ['DATABASE_URL']
        self.db=Database(self.url,schema=self.schema)
        if self.schema:
            self.assertEqual(self.db.engine.get_execution_options().get('schema_translate_map'),{None:self.schema})
            async with self.db.engine.begin() as connection:await connection.run_sync(Base.metadata.drop_all)
        await self.db.initialize(create_schema=True)
        async with self.db.sessions.begin() as session:
            for identifier,role in [('a','customer'),('b','customer'),('s','support'),('t','support'),('z','admin')]:
                session.add(User(id=identifier,display_name=identifier,role=role))
            await session.flush()
            for identifier,login in [('a','customer-a'),('b','customer-b'),('s','support'),('t','support-2'),('z','admin')]:
                session.add(AuthCredential(user_id=identifier,login_name=login,password_hash='fixture-not-for-login'))
        await seed_order_scenarios(self.db)
        self.service=TicketService(PostgresTicketRepository(self.db))

    async def asyncTearDown(self):await self.db.close();self.temp.cleanup()

    async def create_assigned(self):
        row=await self.service.register(identity(),registration())
        return await self.service.assign(identity(),row.ticket_id,assignment('s',row.version))

    async def test_registration_owner_order_and_rollback_without_hidden_user_creation(self):
        row=await self.service.register(identity(),registration(type='refund',order_id='ORDER-A-006'))
        self.assertEqual(row.version,1);self.assertEqual(row.source,'manual_api')
        for body in [registration(type='refund'),registration(type='refund',order_id='ORDER-B-001'),registration(type='account_open')]:
            with self.assertRaises(ApplicationError):await self.service.register(identity(),body)
        self.assertEqual((await self.db.summary())['counts']['tickets'],1)
        with self.assertRaises(IntegrityError):await self.service.register(identity('nonexistent'),registration())
        self.assertEqual((await self.db.summary())['counts']['users'],5)
        with self.assertRaises(ApplicationError) as foreign:await self.service.get(identity('b'),row.ticket_id)
        self.assertEqual(foreign.exception.status_code,404)

    async def test_staff_assignment_transition_and_real_actor_audit(self):
        row=await self.create_assigned();identifier=row.ticket_id
        with self.assertRaises(ApplicationError) as owner_write:await self.service.transition(identity(),identifier,transition('processing',row.version))
        self.assertEqual(owner_write.exception.status_code,403)
        with self.assertRaises(ApplicationError):await self.service.get(identity('z','admin'),identifier)
        for target in ['processing','pending_review','escalated','resolved']:
            row=await self.service.transition(identity('s','support'),identifier,transition(target,row.version))
        row=await self.service.transition(identity(),identifier,transition('closed',row.version))
        self.assertEqual(row.status,'closed');self.assertEqual(row.allowed_transitions,[])
        events=(await self.service.events(identity(),identifier))['events']
        self.assertEqual([event['ticket_version'] for event in events],list(range(1,8)))
        self.assertEqual([event['actor_id'] for event in events],['a','a','s','s','s','s','a'])
        self.assertTrue(all(event['source']=='manual_api' for event in events))
        with self.assertRaises(ApplicationError):await self.service.transition(identity('s','support'),identifier,transition('processing',row.version))

    async def test_reassignment_revokes_old_staff_and_never_grants_global_admin_access(self):
        row=await self.create_assigned()
        self.assertEqual((await self.service.list(identity('s','support'),assigned=True))['total'],1)
        with self.assertRaises(ApplicationError):await self.service.assign(identity('s','support'),row.ticket_id,assignment('t',row.version))
        row=await self.service.assign(identity(),row.ticket_id,assignment('t',row.version))
        with self.assertRaises(ApplicationError) as old:await self.service.get(identity('s','support'),row.ticket_id)
        self.assertEqual(old.exception.status_code,404)
        self.assertEqual((await self.service.get(identity('t','support'),row.ticket_id)).assignee_id,'t')
        with self.assertRaises(ApplicationError):await self.service.get(identity('z','admin'),row.ticket_id)
        row=await self.service.assign(identity(),row.ticket_id,assignment(None,row.version))
        self.assertEqual((await self.service.list(identity('t','support'),assigned=True))['total'],0)
        with self.assertRaises(ApplicationError):await self.service.assign(identity(),row.ticket_id,assignment('b',row.version))
        events=(await self.service.events(identity(),row.ticket_id))['events']
        self.assertEqual(events[-2]['assignee_before'],'s');self.assertEqual(events[-2]['assignee_after'],'t')
        self.assertEqual(events[-1]['event_type'],'unassigned')

    async def test_stale_and_illegal_transitions_do_not_change_ticket_or_events(self):
        row=await self.create_assigned();before=(await self.service.events(identity(),row.ticket_id))['total']
        for body in [transition('resolved',row.version),transition('processing',row.version-1)]:
            with self.assertRaises(ApplicationError) as failed:await self.service.transition(identity('s','support'),row.ticket_id,body)
            self.assertEqual(failed.exception.status_code,409)
        current=await self.service.get(identity(),row.ticket_id)
        self.assertEqual((current.status,current.version),('created',2))
        self.assertEqual((await self.service.events(identity(),row.ticket_id))['total'],before)

    async def test_event_insert_failure_rolls_back_status_and_version(self):
        row=await self.create_assigned();events=(await self.service.events(identity(),row.ticket_id))['events']
        with patch('smart_cs.repositories.tickets.uuid4',return_value=SimpleNamespace(hex=events[0]['id'])):
            with self.assertRaises(IntegrityError):await self.service.transition(identity('s','support'),row.ticket_id,transition('processing',row.version))
        current=await self.service.get(identity(),row.ticket_id)
        self.assertEqual((current.status,current.version),('created',2))
        self.assertEqual((await self.service.events(identity(),row.ticket_id))['total'],2)

    async def test_concurrent_versions_and_assignment_race_have_one_winner(self):
        row=await self.create_assigned()
        if self.db.engine.dialect.name=='postgresql':
            outcomes=await asyncio.gather(self.service.transition(identity('s','support'),row.ticket_id,transition('processing',2)),
                self.service.transition(identity('s','support'),row.ticket_id,transition('pending_review',2)),return_exceptions=True)
            self.assertEqual(sum(not isinstance(item,Exception) for item in outcomes),1)
            self.assertEqual([item.status_code for item in outcomes if isinstance(item,ApplicationError)],[409])
            self.assertEqual((await self.service.events(identity(),row.ticket_id))['total'],3)
            other=await self.create_assigned()
            outcomes=await asyncio.gather(self.service.assign(identity(),other.ticket_id,assignment('t',2)),
                self.service.transition(identity('s','support'),other.ticket_id,transition('processing',2)),return_exceptions=True)
            self.assertEqual(sum(not isinstance(item,Exception) for item in outcomes),1)
            self.assertTrue(all(item.status_code in {404,409} for item in outcomes if isinstance(item,ApplicationError)))
        else:
            await self.service.transition(identity('s','support'),row.ticket_id,transition('processing',2))
            with self.assertRaises(ApplicationError):await self.service.transition(identity('s','support'),row.ticket_id,transition('pending_review',2))

    async def test_event_unique_version_and_database_foreign_key_constraints(self):
        row=await self.service.register(identity(),registration())
        base={'ticket_id':row.ticket_id,'actor_id':'a','event_type':'status_changed','from_status':'created','to_status':'processing'}
        for index,changes in enumerate([{'ticket_version':1},{'ticket_version':0},{'ticket_version':2,'actor_role':'root'},{'ticket_version':2,'actor_id':'missing'}]):
            with self.assertRaises(IntegrityError):
                async with self.db.sessions.begin() as session:session.add(TicketEvent(id=f'bad-{index}',**(base|changes)));await session.flush()
        with self.assertRaises(IntegrityError):
            async with self.db.sessions.begin() as session:await session.execute(update(Ticket).where(Ticket.id==row.ticket_id).values(assignee_id='missing'))

    async def test_new_engine_restores_ticket_and_event_history_and_paging(self):
        row=await self.create_assigned();await self.db.close();self.db=Database(self.url,schema=self.schema)
        self.service=TicketService(PostgresTicketRepository(self.db))
        self.assertEqual((await self.service.get(identity(),row.ticket_id)).version,2)
        page=await self.service.events(identity(),row.ticket_id,limit=1,offset=1)
        self.assertEqual(page['events'][0]['event_type'],'assigned');self.assertEqual(page['total'],2)
        with self.assertRaises(ApplicationError):await self.service.list(identity(),limit=101)
        with self.assertRaises(ApplicationError):await self.service.list(identity(),assigned=True)
        self.assertEqual((await self.service.list(identity('s','support'),assigned=True,status='created'))['total'],1)

    async def test_fixture_seed_retains_edited_tickets_and_event_timeline(self):
        first=await seed_ticket_scenarios(self.db);second=await seed_ticket_scenarios(self.db)
        self.assertEqual((first['inserted'],second['inserted'],second['retained']),(3,0,3))
        row=await self.service.get(identity(),'TK-20261114-9A0001')
        await self.service.transition(identity(),row.ticket_id,transition('closed',row.version))
        await seed_ticket_scenarios(self.db)
        self.assertEqual((await self.service.get(identity(),row.ticket_id)).status,'closed')
        self.assertEqual((await self.service.events(identity(),row.ticket_id))['total'],3)


class TicketApiTests(unittest.TestCase):
    db=auth_support.AuthTests.db
    execute=auth_support.AuthTests.execute
    login=auth_support.AuthTests.login

    def setUp(self):
        auth_support.AuthTests.setUp(self)
        self.a=self.login('alice');self.b=self.login('bob');self.s=self.login('agent');self.z=self.login('admin')

    def create(self):
        response=self.client.post('/api/tickets',headers=self.a,json={'type':'refund','summary':'合成登记申请','order_id':'order-alice','confirm_registration':True})
        self.assertEqual(response.status_code,201,response.text);return response.json()['ticket']

    def test_manual_registration_requires_login_confirmation_and_owned_order(self):
        body={'type':'refund','summary':'仅登记服务申请','order_id':'order-alice','confirm_registration':True}
        self.assertEqual(self.client.post('/api/tickets',json=body).status_code,401)
        for changes in [{'confirm_registration':False},{'confirm_registration':1},{'user_id':'bob'},{'actor_role':'admin'},{'summary':' '}]:
            self.assertEqual(self.client.post('/api/tickets',headers=self.a,json=body|changes).status_code,422)
        self.assertEqual(self.client.post('/api/tickets',headers=self.a,json=body|{'order_id':'order-bob'}).status_code,404)
        row=self.create();identifier=row['ticket_id']
        for path in [f'/api/tickets/{identifier}',f'/api/tickets/{identifier}/events']:
            self.assertEqual(self.client.get(path,headers=self.b,params={'user_id':'alice'}).status_code,404)
        events=self.client.get(f'/api/tickets/{identifier}/events',headers=self.a).json()['events']
        self.assertEqual((events[0]['actor_id'],events[0]['actor_role'],events[0]['ticket_version']),('alice','customer',1))
        self.assertEqual(events[0]['source'],'manual_api')

    def test_assigned_staff_flow_role_restrictions_stale_version_and_terminal_close(self):
        row=self.create();path='/api/tickets/'+row['ticket_id']
        self.assertEqual(self.client.get(path,headers=self.s).status_code,404)
        self.assertEqual(self.client.get('/api/support/tickets',headers=self.a).status_code,403)
        row=self.client.post(path+'/assignment',headers=self.a,json={'assignee_id':'agent','expected_version':1,'reason':'选择客服'}).json()['ticket']
        self.assertEqual(self.client.get('/api/support/tickets',headers=self.s).json()['total'],1)
        self.assertEqual(self.client.get(path,headers=self.z).status_code,404)
        self.assertEqual(self.client.post(path+'/transitions',headers=self.a,json={'target_status':'processing','expected_version':2,'reason':'伪造处理'}).status_code,403)
        for target in ['processing','resolved']:
            response=self.client.post(path+'/transitions',headers=self.s,json={'target_status':target,'expected_version':row['version'],'reason':'客服合成处理'})
            self.assertEqual(response.status_code,200,response.text);row=response.json()['ticket']
        self.assertEqual(self.client.post(path+'/transitions',headers=self.s,json={'target_status':'closed','expected_version':2,'reason':'过期版本'}).status_code,409)
        row=self.client.post(path+'/transitions',headers=self.a,json={'target_status':'closed','expected_version':row['version'],'reason':'用户确认关闭'}).json()['ticket']
        self.assertEqual(row['status'],'closed')
        events=self.client.get(path+'/events',headers=self.a).json()['events']
        self.assertEqual([e['ticket_version'] for e in events],[1,2,3,4,5]);self.assertEqual([e['actor_id'] for e in events],['alice','alice','agent','agent','alice'])

    def test_revoke_assignment_and_live_role_changes_remove_scope(self):
        row=self.create();path='/api/tickets/'+row['ticket_id']
        row=self.client.post(path+'/assignment',headers=self.a,json={'assignee_id':'agent','expected_version':1,'reason':'选择客服'}).json()['ticket']
        self.assertEqual(self.client.get(path,headers=self.s).status_code,200)
        row=self.client.post(path+'/assignment',headers=self.a,json={'assignee_id':None,'expected_version':row['version'],'reason':'撤销客服授权'}).json()['ticket']
        self.assertEqual(self.client.get(path+'/events',headers=self.s).status_code,404)
        row=self.client.post(path+'/assignment',headers=self.a,json={'assignee_id':'agent','expected_version':row['version'],'reason':'重新选择客服'}).json()['ticket']
        async def change_role(db):
            async with db.sessions.begin() as session:await session.execute(update(User).where(User.id=='agent').values(role='customer'))
        self.execute(change_role)
        self.assertEqual(self.client.get(path,headers=self.s).status_code,404)

    def test_live_agent_port_has_no_writer_and_model_create_cannot_commit(self):
        self.assertIsInstance(main.ticket_store,AgentTicketReader);self.assertFalse(hasattr(main.ticket_store,'create'))
        row=self.create()
        async def counts(db):
            async with db.sessions() as session:return await session.scalar(select(func.count()).select_from(Ticket))
        before=self.execute(counts)
        for operation in [{'action':'create','summary':'不应写入','order_id':'order-alice'}, {'action':'query','ticket_id':row['ticket_id'],'ticket_type':None,'priority':'unknown','summary':{'ignored':'not a registration'}},
                          {'action':'query','ticket_id':self.ticket['ticket_id']}]:
            fake=FakeListChatModel(responses=['{"primary_intent":"transaction","confidence":0.99,"suggested_agent":"ticket_handler"}',json.dumps(operation),'{"passed":true,"risk_level":"low"}'])
            graph=create_supervisor_graph(llm=fake,ticket_store=main.ticket_store)
            with patch.object(main,'graph',graph):
                response=self.client.post('/api/chat',headers=self.a,json={'message':'合成接口验证','user_id':'bob'})
            self.assertEqual(response.status_code,200,response.text);answer=response.json()['response']
            if operation['action']=='create':self.assertIn('尚未创建',answer)
            elif operation['ticket_id']==row['ticket_id']:
                self.assertIn('工单查询结果',answer);self.assertIn('已授权的服务工单记录',answer);self.assertIn('不代表退款',answer)
            else:self.assertIn('未找到',answer);self.assertNotIn('Bob private ticket',answer)
            self.assertEqual(self.execute(counts),before)
        self.assertEqual(self.client.post('/api/tools/call',headers=self.a,json={'name':'ticket_create','arguments':{}}).status_code,403)
        self.assertEqual(self.client.post('/api/tools/call',headers=self.a,json={'name':'ticket_transition','arguments':{}}).status_code,403)


if __name__=='__main__':unittest.main()
