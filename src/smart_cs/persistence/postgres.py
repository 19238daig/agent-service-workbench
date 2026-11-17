"""Official saver in a dedicated schema; each execution holds one advisory lock.

Writes use the SAME connection as the lock. A disconnected worker cannot silently
reconnect its saver and overwrite a newly recovered execution's checkpoints.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import re
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import text
from smart_cs.persistence.context import active_saver
from smart_cs.schemas.errors import ApplicationError

CHECKPOINT_SCHEMA='smart_cs_checkpoints'


def checkpoint_schema(database):return database.schema or CHECKPOINT_SCHEMA


def serializer():
    return JsonPlusSerializer(pickle_fallback=False,allowed_json_modules=None,allowed_msgpack_modules=None)


class ScopedPostgresSaver(AsyncPostgresSaver):
    async def aget_tuple(self,config):
        current=active_saver.get()
        return await current.aget_tuple(config) if current else await super().aget_tuple(config)

    async def aput(self,config,checkpoint,metadata,new_versions):
        current=active_saver.get()
        if current is None:raise ApplicationError('CHECKPOINT_WRITE_SCOPE_REQUIRED','执行锁未就绪，已停止保存图状态。',503)
        return await current.aput(config,checkpoint,metadata,new_versions)

    async def aput_writes(self,config,writes,task_id,task_path=''):
        current=active_saver.get()
        if current is None:raise ApplicationError('CHECKPOINT_WRITE_SCOPE_REQUIRED','执行锁未就绪，已停止保存图状态。',503)
        return await current.aput_writes(config,writes,task_id,task_path)


class PostgresCheckpointStore:
    backend='postgresql'
    def __init__(self,database):
        self.database=database;self.schema=checkpoint_schema(database)
        if self.schema!=CHECKPOINT_SCHEMA and not re.fullmatch(r'foundation_verify_[a-f0-9]{12}',self.schema):raise ValueError('Invalid checkpoint schema')
        url=database.engine.url.set(drivername='postgresql').render_as_string(hide_password=False)
        # No public fallback: isolated test tables must never resolve public tables.
        self.pool=AsyncConnectionPool(url,open=False,min_size=1,max_size=8,timeout=3,
            kwargs={'autocommit':True,'prepare_threshold':0,'row_factory':dict_row,
                    'options':f'-c search_path={self.schema} -c statement_timeout=15000','connect_timeout':5})
        self.saver=ScopedPostgresSaver(self.pool,serde=serializer())

    async def open(self):
        await self.pool.open();await self.pool.wait(timeout=10)
        async with self.pool.connection() as conn:
            row=await (await conn.execute('SELECT max(v) AS v FROM checkpoint_migrations')).fetchone()
            if row['v']!=len(AsyncPostgresSaver.MIGRATIONS)-1:raise RuntimeError('Checkpoint migrations are not current')
        return self

    async def close(self):await self.pool.close()

    @asynccontextmanager
    async def lock(self,thread_id):
        key=int.from_bytes(hashlib.sha256(thread_id.encode()).digest()[:8],'big',signed=True)
        async with self.pool.connection() as conn:
            row=await (await conn.execute('SELECT pg_try_advisory_lock(%s) AS acquired',(key,))).fetchone()
            if not row['acquired']:raise ApplicationError('SESSION_BUSY','此会话正在处理另一轮请求，请稍候。',409)
            try:yield AsyncPostgresSaver(conn,serde=serializer())
            finally:
                if not conn.closed:
                    try:await conn.execute('SELECT pg_advisory_unlock(%s)',(key,))
                    except Exception:await conn.close()


class MemoryCheckpointLocks:
    """Explicit SQLite test adapter; never selected for PostgreSQL."""
    backend='memory_test_adapter'
    saver=None
    def __init__(self):self.locks={}
    @asynccontextmanager
    async def lock(self,thread_id):
        lock=self.locks.setdefault(thread_id,asyncio.Lock())
        if lock.locked():raise ApplicationError('SESSION_BUSY','此会话正在处理另一轮请求，请稍候。',409)
        async with lock:yield None
    async def close(self):pass


async def prepare_checkpoint_store(database):
    if database.engine.dialect.name!='postgresql':return {'backend':'memory_test_adapter'}
    schema=checkpoint_schema(database)
    if not database.schema:
        async with database.engine.begin() as conn:await conn.execute(text('CREATE SCHEMA IF NOT EXISTS smart_cs_checkpoints'))
    store=PostgresCheckpointStore(database)
    try:
        await store.pool.open();await store.pool.wait(timeout=10)
        async with store.pool.connection() as conn:
            row=await (await conn.execute('SELECT current_schema() AS name')).fetchone()
            if row['name']!=schema:raise RuntimeError('Checkpoint schema isolation failed')
        await store.saver.setup()
        return {'backend':'postgresql','schema':schema,'migration':len(AsyncPostgresSaver.MIGRATIONS)-1,'pickle_enabled':False}
    finally:await store.close()
