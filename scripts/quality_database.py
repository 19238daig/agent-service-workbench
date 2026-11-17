"""Disposable restricted PG schema. Root knowledge is cloned locally, never mutated."""
import asyncio,json,os,re,secrets
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import create_engine,text,select,delete
from sqlalchemy.engine import make_url
from psycopg import sql
from smart_cs.db.database import Database
from smart_cs.db.models import Base,User,AuthCredential,Order,Ticket,ChatSession,ExecutionRun,ChatMessage,ChatTurn
from smart_cs.schemas.identity import IdentityContext
from smart_cs.persistence.conversations import context_fingerprint
from smart_cs.persistence.context import GRAPH_VERSION
from smart_cs.services.actions import ActionPlanService
from smart_cs.services.orders import OrderService
from smart_cs.services.tickets import TicketService
from smart_cs.repositories.orders import OrderRepository
from smart_cs.repositories.tickets import PostgresTicketRepository
from smart_cs.services.approvals import ApprovalService
from smart_cs.services.execution import ActionExecutionService
from evaluate_quality import OUT,read,write

KB={'knowledge_documents','knowledge_chunks','knowledge_ingestions','retrieval_indexes','retrieval_index_documents','knowledge_embeddings'}


def root_engine():return create_engine(os.environ['DATABASE_URL'].replace('+asyncpg','+psycopg',1),hide_parameters=True)


async def create():
    schema='foundation_verify_'+uuid4().hex[:12];password=secrets.token_urlsafe(32);engine=root_engine()
    try:
        with engine.begin() as c:
            cursor=c.connection.driver_connection.cursor()
            cursor.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(schema),sql.Literal(password)))
            cursor.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(sql.Identifier(schema),sql.Identifier(schema)))
            cursor.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(schema)))
        url=make_url(os.environ['DATABASE_URL']).set(username=schema,password=password).render_as_string(hide_password=False)
        write(OUT/'workflow-db-private.json',{'schema':schema,'url':url,'created':True})
        db=Database(url,schema=schema)
        await db.initialize(create_schema=True)
        with engine.begin() as c:
            # Copy source owner identifiers only as FK stubs, not their personal information.
            c.exec_driver_sql(f'''INSERT INTO "{schema}".users (id,display_name,role,source,created_at)
                SELECT id,'Isolated knowledge owner',role,'evaluation_fixture',created_at FROM public.users
                WHERE id IN (SELECT owner_id FROM public.knowledge_documents WHERE owner_id IS NOT NULL)''')
            for name in ['knowledge_documents','knowledge_ingestions','knowledge_chunks','retrieval_indexes','retrieval_index_documents','knowledge_embeddings']:
                columns=','.join('"'+column.name+'"' for column in Base.metadata.tables[name].columns)
                c.exec_driver_sql(f'INSERT INTO "{schema}"."{name}" ({columns}) SELECT {columns} FROM public."{name}"')
            c.exec_driver_sql('SET LOCAL ROLE "'+schema+'"')
            assert not c.scalar(text("SELECT has_table_privilege(current_user,'public.orders','INSERT,UPDATE,DELETE,TRUNCATE') OR has_schema_privilege(current_user,'public','CREATE')"))
        return db
    except BaseException:
        await cleanup();raise
    finally:engine.dispose()


async def cleanup():
    path=OUT/'workflow-db-private.json'
    if not path.exists():return
    state=read(path);schema=state['schema'];assert re.fullmatch(r'foundation_verify_[a-f0-9]{12}',schema)
    engine=root_engine()
    try:
        with engine.begin() as c:
            cursor=c.connection.driver_connection.cursor()
            cursor.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
            cursor.execute(sql.SQL('REVOKE USAGE ON SCHEMA public FROM {}').format(sql.Identifier(schema)))
            cursor.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(schema)))
        write(OUT/'workflow-database-cleanup.json',{'passed':True,'restricted_role':True,'temporary_schema_removed':True,'knowledge_snapshot_local_only':True})
        path.unlink()
    finally:engine.dispose()


class Fixture:
    def __init__(self,db):
        self.db=db;self.actor=IdentityContext(subject_id='phase26-customer',authenticated=True,mode='authenticated_session')
        self.other=self.actor.model_copy(update={'subject_id':'phase26-other'})
        self.staff=self.actor.model_copy(update={'subject_id':'phase26-support','role':'support'})
        self.admin=self.actor.model_copy(update={'subject_id':'phase26-admin','role':'admin'})
        self.order='ORDER-EVAL-A-001';self.foreign='ORDER-EVAL-B-001';self.ticket='TK-EVAL-001'

    async def reset(self,clock):
        self.now=clock
        async with self.db.sessions.begin() as s:
            for table in reversed(Base.metadata.sorted_tables):
                if table.name not in KB and table.name!='users':await s.execute(delete(table))
            for actor in [self.actor,self.other,self.staff,self.admin]:
                found=await s.get(User,actor.subject_id)
                if not found:s.add(User(id=actor.subject_id,display_name='Phase26 synthetic actor',role=actor.role,source='evaluation_fixture'))
            await s.flush()
            for actor in [self.actor,self.other,self.staff,self.admin]:s.add(AuthCredential(user_id=actor.subject_id,login_name=actor.subject_id,password_hash='not-a-login-fixture',enabled=True))
            for identifier,owner,name in [(self.order,self.actor,'Synthetic product'),(self.foreign,self.other,'FOREIGN_FACT_CANARY_26')]:
                s.add(Order(id=identifier,user_id=owner.subject_id,product_name=name,paid_amount=Decimal('500.00'),status='pending_shipment',
                    paid_at=clock-timedelta(days=1),refunded_amount=Decimal('100.01'),refund_reserved_amount=Decimal('49.99'),
                    return_policy='standard_7d',return_condition='intact',fact_version=3,source='evaluation_fixture'))
            await s.flush()
            s.add(Ticket(id=self.ticket,user_id=self.actor.subject_id,order_id=self.order,type='refund',priority='medium',status='processing',
                        summary='Synthetic existing ticket',details='Evaluation only',version=2,assignee_id=self.staff.subject_id,requested_amount=Decimal('49.99')))
        self.actions=ActionPlanService(OrderService(OrderRepository(self.db),clock=lambda:self.now),TicketService(PostgresTicketRepository(self.db)),clock=lambda:self.now)
        self.approvals=ApprovalService(self.db,self.actions,clock=lambda:self.now)
        self.executions=ActionExecutionService(self.db,clock=lambda:self.now)

    async def turn(self,actor,message,session_id=None):
        session_id=session_id or uuid4().hex;run=uuid4().hex;turn=uuid4().hex;message_id=uuid4().hex
        async with self.db.sessions.begin() as s:
            session=await s.get(ChatSession,session_id)
            if session is None:s.add(ChatSession(id=session_id,user_id=actor.subject_id,thread_id=uuid4().hex,graph_version=GRAPH_VERSION,next_sequence=2));await s.flush()
            s.add(ExecutionRun(id=run,request_id=uuid4().hex,trace_id=uuid4().hex,session_id=session_id,subject_id=actor.subject_id,status='running',snapshot={}));await s.flush()
            s.add(ChatMessage(id=message_id,session_id=session_id,user_id=actor.subject_id,role='user',sequence=1 if session is None else session.next_sequence,content=message,run_id=run));await s.flush()
            context=await context_fingerprint(s,actor)
            s.add(ChatTurn(id=turn,session_id=session_id,user_id=actor.subject_id,current_run_id=run,user_message_id=message_id,
                graph_version=GRAPH_VERSION,authorization_role=actor.role,context_fingerprint=context,model_fingerprint='0'*64,status='running'))
            if session is not None:session.next_sequence+=1
        return {'session_id':session_id,'turn_id':turn,'run_id':run,'user_id':actor.subject_id,'identity_context':actor.model_dump(),
                'business_context_fingerprint':context,'request_id':uuid4().hex,'trace_id':uuid4().hex}
