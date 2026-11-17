"""Empty/seeded PostgreSQL migration acceptance in disposable isolated schemas."""
import asyncio
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine,inspect,select,text,delete,bindparam
from sqlalchemy.exc import DBAPIError
from smart_cs.settings import PROJECT_ROOT,ENV_PATH
from smart_cs.db.database import Database
from smart_cs.db.models import Base,User,Order,Ticket,TicketEvent,ChatSession,ChatMessage,KnowledgeChunk,ExecutionRun,ModelCallRecord,utcnow
from smart_cs.db.seed import seed_database
from smart_cs.db.tickets import DatabaseTicketStore
from smart_cs.repositories.conversations import ConversationRepository


def config(connection,schema):
    value=Config(str(PROJECT_ROOT/'alembic.ini'));value.attributes['connection']=connection;value.attributes['version_table_schema']=schema
    return value


def migrate(engine,schema,target):
    with engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", public')
        command.upgrade(config(connection,schema),target)


async def fingerprints(database,legacy_order_projection=False,exclude_order_ids=None,legacy_ticket_projection=False,exclude_ticket_ids=None,legacy_conversation_projection=False,legacy_ingestion_projection=False,legacy_dense_projection=False,legacy_execution_projection=False):
    async with database.engine.connect() as connection:
        names=await connection.run_sync(lambda conn:inspect(conn).get_table_names(schema=database.schema))
    result={}
    async with database.sessions() as session:
        for name in sorted(set(names)&set(Base.metadata.tables)):
            if legacy_execution_projection and name=='action_executions':continue
            if legacy_conversation_projection and name=='chat_turns':continue
            if legacy_ingestion_projection and name=='knowledge_ingestions':continue
            if legacy_dense_projection and name=='retrieval_index_documents':continue
            relation=f'"{database.schema}"."{name}"' if database.schema else '"'+name+'"'
            projection="to_jsonb(t) - 'access_scope' - 'owner_id'" if name=='knowledge_documents' else 'to_jsonb(t)'
            if name=='orders' and legacy_order_projection:
                projection="to_jsonb(t) - 'shipped_at' - 'delivered_at' - 'refunded_amount' - 'refund_reserved_amount' - 'return_policy' - 'return_condition' - 'fact_version'"
            if legacy_ticket_projection and name=='tickets':projection="to_jsonb(t) - 'assignee_id'"
            if legacy_ticket_projection and name=='ticket_events':projection="to_jsonb(t) - 'ticket_version' - 'actor_role' - 'source' - 'note' - 'assignee_before' - 'assignee_after'"
            if legacy_conversation_projection and name=='chat_sessions':projection="to_jsonb(t) - 'thread_id' - 'active_run_id'"
            if legacy_conversation_projection and name=='chat_messages':projection="to_jsonb(t) - 'run_id' - 'metadata_json'"
            if legacy_ingestion_projection and name=='knowledge_chunks':projection="to_jsonb(t) - 'locator'"
            if legacy_dense_projection and name=='knowledge_documents':projection="to_jsonb(t) - 'access_scope' - 'owner_id' - 'effective_until'"
            if legacy_execution_projection and name=='tickets':projection=f"({projection}) - 'requested_amount'"
            if legacy_execution_projection and name=='ticket_events':projection=f"({projection}) - 'action_id'"
            excludes=exclude_order_ids if name=='orders' else exclude_ticket_ids if name in {'tickets','ticket_events'} else None
            column='ticket_id' if name=='ticket_events' else 'id'
            statement=text(f"SELECT count(*), md5(COALESCE(string_agg(({projection})::text, E'\\n' ORDER BY ({projection})::text),'')) FROM {relation} t"+
                (f' WHERE t.{column} NOT IN :excluded' if excludes else ''))
            if excludes:
                statement=statement.bindparams(bindparam('excluded',expanding=True))
                row=(await session.execute(statement,{'excluded':list(excludes)})).one()
            else:row=(await session.execute(statement)).one()
            result[name]={'count':row[0],'digest':row[1]}
    return result


async def reject(database,row,expected_state):
    try:
        async with database.sessions.begin() as session:session.add(row);await session.flush()
    except DBAPIError as error:
        state=getattr(error.orig,'sqlstate',None)
        if state!=expected_state:raise AssertionError('Unexpected SQLSTATE '+str(state)) from None
        return True
    raise AssertionError('Constraint accepted invalid row')


def new_order(identifier,**kwargs):
    return Order(**{'id':identifier,'user_id':'demo-user-001','product_name':'合成约束验证商品','paid_amount':Decimal('99.10'),
                   'currency':'CNY','status':'delivered','paid_at':utcnow(),'source':'verification_fixture',**kwargs})


async def exercise(database):
    checks={}
    for name,values,state in [
        ('negative_amount',{'paid_amount':Decimal('-0.01')},'23514'),
        ('nan_amount',{'paid_amount':Decimal('NaN')},'23514'),
        ('money_overflow',{'paid_amount':Decimal('10000000000')},'22003'),
        ('currency',{'currency':'USD'},'23514'),('order_status',{'status':'invented'},'23514'),
        ('orphan_order',{'user_id':'missing-user'},'23503')]:
        checks[name]=await reject(database,new_order('bad-'+name,**values),state)
    async with database.sessions.begin() as session:session.add(new_order('precision-order'))
    async with database.sessions() as session:checks['decimal_roundtrip']=(await session.get(Order,'precision-order')).paid_amount==Decimal('99.10')
    checks['ticket_order_owner']=await reject(database,Ticket(id='bad-owner-ticket',user_id='demo-user-002',order_id='precision-order',type='refund',priority='medium',status='created',summary='合成跨归属验证',details=''),'23503')
    checks['ticket_version']=await reject(database,Ticket(id='bad-version-ticket',user_id='demo-user-001',type='general',priority='medium',status='created',summary='合成',details='',version=0),'23514')
    checks['event_status']=await reject(database,TicketEvent(id='bad-event',ticket_id='TK-20261113-000001',actor_id='demo-user-001',event_type='test',to_status='invented'),'23514')
    repo=ConversationRepository(database);identifier=await repo.create('demo-user-001','合成并发验证')
    for name,values,state in [
        ('message_wrong_owner',{'user_id':'demo-user-002'},'23503'),('orphan_message',{'session_id':'missing-session'},'23503'),
        ('message_role',{'role':'tool'},'23514'),('message_sequence',{'sequence':0},'23514'),('message_content',{'content':'   '},'23514')]:
        data={'id':'bad-'+name,'session_id':identifier,'user_id':'demo-user-001','sequence':1,'role':'user','content':'合成',**values}
        checks[name]=await reject(database,ChatMessage(**data),state)
    messages=await asyncio.gather(*(repo.append(identifier,'demo-user-001','user',f'合成消息{i}') for i in range(8)))
    checks['concurrent_message_sequence']=sorted(m['sequence'] for m in messages)==list(range(1,9))
    checks['message_unique_position']=await reject(database,ChatMessage(id='duplicate-sequence',session_id=identifier,user_id='demo-user-001',sequence=1,role='user',content='合成'),'23505')
    try:await repo.append(identifier,'demo-user-001','assistant','重复主键验证',message_id=messages[0]['id'])
    except DBAPIError:pass
    else:raise AssertionError('Duplicate message id accepted')
    checks['sequence_rollback']=(await repo.append(identifier,'demo-user-001','assistant','回滚后验证'))['sequence']==9
    checks['history_owner_filter']=await repo.history(identifier,'demo-user-002') is None
    checks['legacy_null_uniqueness']=await reject(database,KnowledgeChunk(id='duplicate-legacy',document_id='kb-v1-refund_policy',ordinal=0,chunk_set=None,content='合成',content_hash='a'*64),'23505')
    async with database.sessions.begin() as session:
        run=ExecutionRun(id='f'*32,request_id='1'*32,trace_id='2'*32,session_id='legacy-label',subject_id='demo-user-001',status='running',snapshot={})
        session.add(run)
    checks['call_correlation']=await reject(database,ModelCallRecord(id='e'*32,run_id='f'*32,request_id='3'*32,trace_id='2'*32,kind='chat',agent_role='test',operation='test',model='test',status='succeeded',duration_ms=0,payload={}),'23503')
    checks['run_status']=await reject(database,ExecutionRun(id='0'*32,request_id='1'*32,trace_id='2'*32,session_id='s',subject_id='test',status='invented',snapshot={}), '23514')
    # Two inserts share a transaction: invalid child must roll back the valid order.
    try:
        async with database.sessions.begin() as session:
            session.add(new_order('must-rollback'));await session.flush()
            session.add(Ticket(id='rollback-child',user_id='demo-user-001',order_id='must-rollback',type='general',priority='invalid',status='created',summary='合成',details=''));await session.flush()
    except DBAPIError:pass
    else:raise AssertionError('Invalid transaction committed')
    async with database.sessions() as session:checks['transaction_rollback']=await session.get(Order,'must-rollback') is None
    await repo.append('fixture-session-001','demo-user-001','user','保留追加历史')
    history=await repo.history('fixture-session-001','demo-user-001');await seed_database(database)
    checks['session_seed_preserves_edits']=history==await repo.history('fixture-session-001','demo-user-001')
    return checks


async def seeded_upgrade(url,engine,schema):
    db=Database(url,schema=schema)
    try:
        # Populate the old document shape using reflection; current ORM defaults
        # include ACL columns that deliberately do not exist at revision 0003.
        from sqlalchemy import Table,MetaData
        from smart_cs.datasets import DATA_ROOT
        from smart_cs.db.models import KnowledgeDocument
        documents=json.loads((DATA_ROOT/'knowledge/documents.json').read_text(encoding='utf-8'))
        async with db.engine.begin() as connection:
            from datetime import datetime,timedelta,timezone
            fixed=datetime(2026,11,13,tzinfo=timezone.utc)
            users=await connection.run_sync(lambda conn:Table('users',MetaData(),schema=schema,autoload_with=conn))
            orders=await connection.run_sync(lambda conn:Table('orders',MetaData(),schema=schema,autoload_with=conn))
            # Current Order ORM defaults contain step-eight columns. Populate the
            # actual revision-0003 shape using reflected, explicitly scoped tables.
            for i in range(1,21):
                await connection.execute(users.insert().values(id=f'demo-user-{i:03d}',display_name=f'合成演示用户{i:03d}',role='customer',source='synthetic_fixture',created_at=fixed))
            for i in range(1,201):
                await connection.execute(orders.insert().values(id=f'DEMO-{i:06d}',user_id=f'demo-user-{(i-1)%20+1:03d}',
                    product_name='虚构演示耳机' if i%2 else '虚构演示配件',paid_amount=Decimal('99.00')+Decimal(i),currency='CNY',
                    status=['pending_shipment','shipped','delivered','cancelled'][i%4],paid_at=fixed-timedelta(days=i%30),source='synthetic_fixture'))
            tickets=await connection.run_sync(lambda conn:Table('tickets',MetaData(),schema=schema,autoload_with=conn))
            events=await connection.run_sync(lambda conn:Table('ticket_events',MetaData(),schema=schema,autoload_with=conn))
            for i in range(1,501):
                order_index=(i-1)%200+1;owner=f'demo-user-{(order_index-1)%20+1:03d}';identifier=f'TK-20261113-{i:06X}'
                status=['created','processing','pending_review','resolved','closed'][i%5]
                await connection.execute(tickets.insert().values(id=identifier,user_id=owner,order_id=f'DEMO-{order_index:06d}',type='refund' if i%2 else 'complaint',
                    priority='medium',status=status,summary=f'合成演示工单{i:03d}',details='用于本地开发和集成验证，不对应真实客户或真实支付。',source='synthetic_fixture',created_at=fixed-timedelta(minutes=i),updated_at=fixed,version=1))
                await connection.execute(events.insert().values(id=f'fixture-created-{i:06d}',ticket_id=identifier,actor_id=owner,event_type='fixture_snapshot',from_status=None,to_status=status,created_at=fixed))
            table=await connection.run_sync(lambda conn:Table('knowledge_documents',MetaData(),schema=schema,autoload_with=conn))
            for doc in documents:
                await connection.execute(table.insert().values(**doc))
        async with db.sessions.begin() as session:
            for doc in documents:
                session.add(KnowledgeChunk(id=doc['id']+'-000',document_id=doc['id'],ordinal=0,content=doc['content'],content_hash=doc['content_hash'],embedding_status='pending'))
        await seed_database(db,include_conversations=False)
        async with db.engine.begin() as connection:
            await connection.execute(tickets.update().where(tickets.c.id=='TK-20261113-000001').values(status='resolved',version=2,updated_at=fixed+timedelta(seconds=1)))
            await connection.execute(events.insert().values(id='legacy-edit-event',ticket_id='TK-20261113-000001',actor_id='demo-user-001',event_type='status_changed',
                from_status='processing',to_status='resolved',created_at=fixed+timedelta(seconds=1)))
        async with db.engine.begin() as connection:
            await connection.execute(orders.insert().values(id='legacy-invalid',user_id='demo-user-001',product_name='合成验证商品',paid_amount=Decimal('99.10'),currency='USD',status='delivered',paid_at=fixed,source='test_fixture'))
        blocked=False
        try:migrate(engine,schema,'head')
        except RuntimeError:blocked=True
        async with db.sessions() as session:
            unchanged=(await session.scalar(select(Order.currency).where(Order.id=='legacy-invalid')))=='USD'
            version=await session.scalar(text('SELECT version_num FROM alembic_version'))
        if not blocked or not unchanged or version!='0003_run_observability':raise AssertionError('Preflight did not safely stop upgrade')
        # Remove only our intentionally invalid test fixture in this disposable schema.
        async with db.sessions.begin() as session:await session.execute(delete(Order).where(Order.id=='legacy-invalid'))
        before=await fingerprints(db,legacy_order_projection=True,legacy_ticket_projection=True,legacy_ingestion_projection=True,legacy_dense_projection=True,legacy_execution_projection=True)
        migrate(engine,schema,'head')
        after=await fingerprints(db,legacy_order_projection=True,legacy_ticket_projection=True,legacy_ingestion_projection=True,legacy_dense_projection=True,legacy_execution_projection=True)
        preserved=all(after[name]==value for name,value in before.items())
        if not preserved:raise AssertionError('Legacy facts changed during migration')
        first=await seed_database(db);second=await seed_database(db)
        if first['database']['counts']!=second['database']['counts']:raise AssertionError('Seed is not idempotent')
        checks=await exercise(db)
        async with db.sessions() as session:checks['edited_ticket_preserved']=(await session.get(Ticket,'TK-20261113-000001')).status=='resolved'
        if not all(checks.values()):raise AssertionError('A core constraint check failed')
        return {'passed':True,'legacy_rows_preserved':preserved,'preflight_refused_invalid_rows_without_rewriting':True,
                'seed_counts':first['database']['counts'],'checks':checks}
    finally:await db.close()


def main():
    load_dotenv(ENV_PATH);url=os.environ['DATABASE_URL']
    if not url.startswith('postgresql+asyncpg://'):raise RuntimeError('Configured PostgreSQL required')
    engine=create_engine(url.replace('+asyncpg','+psycopg',1),hide_parameters=True)
    schemas=[];report={'passed':False,'real_postgresql':True,'isolated_schemas':True}
    try:
        for _ in range(2):
            schema='foundation_verify_'+uuid4().hex[:12]
            if not re.fullmatch('foundation_verify_[a-f0-9]{12}',schema):raise RuntimeError('Unsafe verification schema')
            with engine.begin() as connection:connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            schemas.append(schema)
        migrate(engine,schemas[0],'head')
        with engine.begin() as connection:
            tables=set(inspect(connection).get_table_names(schema=schemas[0]))
            report['empty_schema_to_head']=set(Base.metadata.tables)<=tables
        if not report['empty_schema_to_head']:raise AssertionError('Missing core tables')
        migrate(engine,schemas[1],'0003_run_observability')
        report['seeded_upgrade']=asyncio.run(seeded_upgrade(url,engine,schemas[1]))
        report['passed']=True
    except Exception as error:
        report['error_type']=type(error).__name__
        report['sqlstate']=getattr(getattr(error,'orig',None),'sqlstate',None)
        import traceback
        report['locations']=[{'file':Path(frame.filename).name,'line':frame.lineno} for frame in traceback.extract_tb(error.__traceback__) if str(PROJECT_ROOT) in frame.filename][:8]
        if isinstance(error,AssertionError):report['assertion']=str(error)
    finally:
        for schema in schemas:
            with engine.begin() as connection:connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()
        output=PROJECT_ROOT/'.local/database';output.mkdir(parents=True,exist_ok=True)
        (output/'core-schema-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
