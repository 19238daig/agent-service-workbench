"""Bounded attempts, PostgreSQL session locks and fenced business transactions."""
import asyncio
import hashlib
import re
import socket
from contextlib import suppress
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import select,text,event
from sqlalchemy.ext.asyncio import async_sessionmaker
from smart_cs.background.service import now,expired,record,authorize,digest
from smart_cs.db.models import BackgroundJob as Job
from smart_cs.schemas.errors import ApplicationError

class JobStopped(ApplicationError):
    def __init__(self):super().__init__('JOB_STOPPED','任务已经取消或失去执行权。',409)

class Context:
    def __init__(self,db,job,token):self.db=db;self.job=job;self.token=token
    async def receipt(self,result):
        # Cancellation cannot erase a document/index that already committed.
        async with self.db.sessions.begin() as s:
            job=await s.scalar(select(Job).where(Job.id==self.job.id).with_for_update())
            if job.attempt_token!=self.token or job.status not in {'running','cancel_requested'}:raise JobStopped()
            job.checkpoint={**job.checkpoint,'committed_result':result};job.version+=1
            record(s,job,'effect_committed')
    async def progress(self,stage,progress=0,total=0,checkpoint=None):
        async with self.db.sessions.begin() as s:
            job=await s.scalar(select(Job).where(Job.id==self.job.id).with_for_update())
            if job.attempt_token!=self.token or job.status!='running':raise JobStopped()
            await authorize(s,job.user_id,worker=self.db.schema is None and self.db.engine.dialect.name=='postgresql')
            job.stage=stage;job.progress=progress;job.total=total
            if checkpoint is not None:job.checkpoint=checkpoint
            job.version+=1;record(s,job,'progress',stage=stage,progress=progress,total=total)
    async def heartbeat(self):
        while True:
            await asyncio.sleep(5)
            async with self.db.sessions.begin() as s:
                job=await s.scalar(select(Job).where(Job.id==self.job.id).with_for_update())
                if job.attempt_token!=self.token or job.status not in {'running','cancel_requested'}:return
                job.heartbeat_at=now();job.lease_until=now()+timedelta(seconds=30)

class Worker:
    def __init__(self,database,handler=None):
        self.db=database
        if handler is None:
            from smart_cs.background.handlers import perform
            handler=perform
        self.handler=handler
    async def run(self,identifier):
        if not re.fullmatch('[a-f0-9]{32}',identifier):return {'status':'invalid'}
        postgres=self.db.engine.dialect.name=='postgresql'
        if not postgres:raise RuntimeError('Worker execution requires PostgreSQL session locks')
        key=int.from_bytes(hashlib.sha256(('smart-cs-job:'+identifier).encode()).digest()[:8],signed=True)
        async with self.db.engine.connect() as connection:
            locked=await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'),{'key':key});pid=await connection.scalar(text('SELECT pg_backend_pid()'));await connection.commit()
            if not locked:return {'status':'duplicate_running'}
            try:return await self._run(identifier,connection,pid)
            finally:
                with suppress(Exception):
                    await connection.rollback();await connection.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':key});await connection.commit()
    async def _run(self,identifier,connection,pid):
        async with self.db.sessions.begin() as s:
            job=await s.scalar(select(Job).where(Job.id==identifier).with_for_update())
            if job is None:return {'status':'missing'}
            if job.status in {'succeeded','failed','cancelled'}:return {'status':job.status,'duplicate':True}
            if job.status=='cancel_requested':
                job.status='cancelled';job.version+=1;record(s,job,'cancelled');return {'status':'cancelled'}
            if job.status=='retry_wait' and not expired(job.updated_at+timedelta(seconds=5*job.attempt)):return {'status':'retry_wait'}
            if job.attempt>=job.max_attempts:
                job.status='failed';job.error_code='JOB_ATTEMPTS_EXHAUSTED';job.version+=1;record(s,job,'failed',code=job.error_code);return {'status':'failed'}
            try:await authorize(s,job.user_id,worker=self.db.schema is None)
            except ApplicationError:
                job.status='failed';job.error_code='JOB_ADMIN_REVOKED';job.version+=1;record(s,job,'failed',code=job.error_code);return {'status':'failed'}
            if digest(job.payload)!=job.payload_hash:
                job.status='failed';job.error_code='JOB_PAYLOAD_CHANGED';job.version+=1;record(s,job,'failed',code=job.error_code);return {'status':'failed'}
            token=uuid4().hex;job.attempt+=1;job.attempt_token=token;job.status='running';job.stage='starting';job.worker=socket.gethostname()
            job.heartbeat_at=now();job.lease_until=now()+timedelta(seconds=30);job.version+=1;record(s,job,'started',attempt=job.attempt)
        context=Context(self.db,job,token)
        sessions=async_sessionmaker(connection,expire_on_commit=False)
        # All business writes stay on the connection holding the session lock.
        # Reconnection, cancellation, role revocation or another token rolls back.
        def guard(session):
            conn=session.connection()
            if conn.scalar(text('SELECT pg_backend_pid()'))!=pid:raise JobStopped()
            row=conn.execute(select(Job.status,Job.attempt_token).where(Job.id==identifier).with_for_update(read=True)).one()
            if row!=( 'running',token):raise JobStopped()
            if self.db.schema is None:
                allowed=conn.scalar(text('SELECT smart_cs_worker_admin(:actor)'),{'actor':job.user_id})
            else:
                from smart_cs.db.models import User,AuthCredential
                allowed=conn.scalar(select(User.id).join(AuthCredential).where(User.id==job.user_id,User.role=='admin',AuthCredential.enabled.is_(True)).with_for_update(read=True))
            if not allowed:raise ApplicationError('JOB_ADMIN_REVOKED','管理员权限已撤回。',403)
        # Instance events keep other API sessions untouched.
        def guarded_session(*args,**kwargs):
            session=sessions(*args,**kwargs);event.listen(session.sync_session,'before_commit',guard);return session
        class Factory:
            def __call__(self):return guarded_session()
            def begin(self):
                from contextlib import asynccontextmanager
                @asynccontextmanager
                async def transaction():
                    async with guarded_session() as s:
                        async with s.begin():yield s
                return transaction()
        fenced=SimpleNamespace(engine=self.db.engine,sessions=Factory(),schema=self.db.schema)
        beat=asyncio.create_task(context.heartbeat())
        result=None;error=None
        try:result=await self.handler(context,fenced)
        except Exception as exc:
            # Finite known error codes; never raw stack/SQL/provider text in state.
            code=getattr(exc,'code','JOB_EXECUTION_FAILED')
            error=code if re.fullmatch('[A-Z0-9_]{1,64}',code) else 'JOB_EXECUTION_FAILED'
        finally:
            beat.cancel()
            with suppress(asyncio.CancelledError):await beat
        async with self.db.sessions.begin() as s:
            current=await s.scalar(select(Job).where(Job.id==identifier).with_for_update())
            if current.attempt_token!=token:raise JobStopped()
            if current.status=='cancel_requested':
                current.status='cancelled';current.result=current.checkpoint.get('committed_result')
            elif error:
                # Validation/cancellation/authority errors require operator action.
                retryable=error=='JOB_EXECUTION_FAILED'
                current.status='retry_wait' if retryable and current.attempt<current.max_attempts else 'failed'
                current.error_code=error;current.next_dispatch_at=now()+timedelta(seconds=5*current.attempt)
            else:current.status='succeeded';current.result=result;current.stage='complete';current.progress=current.total;current.error_code=None
            current.lease_until=None;current.version+=1;record(s,current,current.status,code=current.error_code)
            return {'status':current.status,'id':identifier}
