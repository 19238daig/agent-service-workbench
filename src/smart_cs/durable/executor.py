"""API-lifetime supervisor, SQL-durable commands, independent of HTTP subscribers.

Multiple API processes arbitrate using the SAME thread lock as checkpoint writes.
Queued work survives process restarts; abandoned running work requires explicit
recovery, since an in-flight provider call cannot be made exactly-once by SSE.
"""
import asyncio
import logging
import hashlib
import os
from contextlib import aclosing,suppress
from datetime import timedelta
from time import monotonic
from uuid import uuid4
from sqlalchemy import select,or_,and_
from smart_cs.db.models import RunControl,ExecutionRun,ChatSession,ChatTurn,ChatMessage,RunEvent,ModelCallRecord,utcnow
from smart_cs.durable.context import run_attempt
from smart_cs.durable.store import actor,append,EventStore,ACTIVE,fail
from smart_cs.observability.context import RequestCorrelation,usage_summary
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.chat import ChatRequest
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.chat import PreparedRun

class RunExecutor:
    def __init__(self,database,service_factory,*,concurrency=None,max_seconds=None):
        self.db=database;self.factory=service_factory;self.tasks={};self.loop=None
        self.concurrency=concurrency or int(os.getenv('RUN_EXECUTOR_CONCURRENCY','2'))
        self.max_seconds=max_seconds or int(os.getenv('RUN_MAX_SECONDS','600'))
        if not 1<=self.concurrency<=4 or not 1<=self.max_seconds<=1800:raise ValueError('Invalid executor limits')
    def start(self):self.loop=asyncio.create_task(self.serve(),name='durable-run-supervisor')
    async def close(self):
        if self.loop:
            self.loop.cancel()
            with suppress(asyncio.CancelledError):await self.loop
        running=list(self.tasks.values())
        for task in running:task.cancel()
        await asyncio.gather(*running,return_exceptions=True)
    async def serve(self):
        while True:
            try:await self.scan()
            except Exception as error:logging.getLogger(__name__).warning('Run polling unavailable: %s',type(error).__name__)
            await asyncio.sleep(.5)
    async def scan(self):
        for identifier,task in list(self.tasks.items()):
            if task.done():
                with suppress(asyncio.CancelledError,Exception):task.result()
                self.tasks.pop(identifier,None)
        slots=self.concurrency-len(self.tasks)
        if slots<=0:return
        async with self.db.sessions() as s:
            due=or_(RunControl.status=='queued',and_(RunControl.status.in_(['running','cancel_requested']),or_(RunControl.lease_until.is_(None),RunControl.lease_until<utcnow())))
            identifiers=list((await s.scalars(select(RunControl.run_id).where(due).order_by(RunControl.created_at).limit(20))).all())
        for identifier in identifiers:
            if slots<=0:break
            if identifier in self.tasks:continue
            self.tasks[identifier]=asyncio.create_task(self.execute(identifier),name='run-'+identifier);slots-=1
    async def prepared(self,s,control,service,saver):
        run=await s.get(ExecutionRun,control.run_id);turn=await s.get(ChatTurn,control.turn_id);row=await s.get(ChatSession,control.session_id)
        if not turn or turn.current_run_id!=control.run_id or row.active_run_id!=control.run_id:raise fail('RUN_SUPERSEDED')
        message=await s.get(ChatMessage,turn.user_message_id)
        identity=IdentityContext(subject_id=control.user_id,role=turn.authorization_role,mode='authenticated_session',authenticated=True,auth_session_id=control.auth_session_id)
        return PreparedRun(identity,control.session_id,RequestCorrelation(run.request_id,run.trace_id,run.id),run.id,
            persistent=True,thread_id=row.thread_id,turn_id=turn.id,user_message_id=turn.user_message_id,message=message.content,
            kind=control.kind,resumed_from_run_id=run.snapshot.get('resumed_from_run_id'),execution_saver=saver,
            record=dict(run.snapshot),context_hash=turn.context_fingerprint,durable=True,attempt_token=control.attempt_token or '')
    async def execute(self,identifier):
        service=self.factory();runtime=service.conversations
        if runtime is None:return
        async with self.db.sessions() as s:
            control=await s.get(RunControl,identifier)
            if not control or control.status not in ACTIVE:return
            row=await s.get(ChatSession,control.session_id);thread=row.thread_id
        try:
            async with runtime.store.lock(thread) as saver:
                failure=None;abandoned=False
                async with self.db.sessions.begin() as s:
                    control=await s.scalar(select(RunControl).where(RunControl.run_id==identifier).with_for_update())
                    if control.status not in ACTIVE:return
                    p=await self.prepared(s,control,service,saver)
                    if control.status!='queued':abandoned=True
                    else:
                        control.attempt_token=uuid4().hex;p.attempt_token=control.attempt_token
                        try:
                            await actor(s,control,p.identity)
                            row=await s.get(ChatSession,p.session_id);turn=await s.get(ChatTurn,p.turn_id)
                            reason=await runtime.compatibility(s,row,turn if p.kind=='resume' else None,p.identity)
                            if not reason and p.kind=='new':
                                from smart_cs.persistence.conversations import model_fingerprint,context_fingerprint
                                if turn.model_fingerprint!=model_fingerprint(service.workflow):reason='RECOVERY_MODELS_CHANGED'
                                elif turn.context_fingerprint!=await context_fingerprint(s,p.identity):reason='RECOVERY_CONTEXT_CHANGED'
                            if reason:raise fail(reason,'上下文已变化，请核实后显式恢复或新建对话。')
                        except ApplicationError as error:failure=error.code
                        control.status='running';control.heartbeat_at=utcnow();control.lease_until=utcnow()+timedelta(seconds=30)
                        run=await s.get(ExecutionRun,identifier);run.status='running';p.record.update(status='running');run.snapshot=dict(p.record)
                        await append(s,control,{'type':'running','run_id':identifier,'status':'running'})
                binding={'run_id':identifier,'token':p.attempt_token}
                if saver is not None and hasattr(saver,'conn') and hasattr(saver.conn,'info'):
                    binding.update(lock_pid=saver.conn.info.backend_pid,checkpoint_connection=saver.conn,
                        lock_key=int.from_bytes(hashlib.sha256(thread.encode()).digest()[:8],'big',signed=True))
                token=run_attempt.set(binding)
                try:
                    if abandoned:
                        await self.interrupt(service,p);return
                    if failure:
                        await runtime.finish(p,{**p.record,'status':'failed','error_code':failure},[],None);return
                    work=asyncio.create_task(self.consume(service,p))
                    monitor=asyncio.create_task(self.monitor(identifier,p.attempt_token))
                    try:
                        done,_=await asyncio.wait({work,monitor},return_when=asyncio.FIRST_COMPLETED)
                        if monitor in done and not work.done():work.cancel()
                        with suppress(asyncio.CancelledError):await work
                    finally:
                        monitor.cancel();work.cancel()
                        await asyncio.gather(monitor,work,return_exceptions=True)
                        async with self.db.sessions() as s:current=await s.get(RunControl,identifier)
                        if current.status in ACTIVE:await self.interrupt(service,p)
                finally:run_attempt.reset(token)
        except ApplicationError as error:
            if error.code!='SESSION_BUSY':logging.getLogger(__name__).warning('Run stopped: %s',error.code)
        except Exception as error:logging.getLogger(__name__).warning('Run executor unavailable: %s',type(error).__name__)
    async def interrupt(self,service,p):
        async with self.db.sessions() as s:
            calls=[c.payload for c in (await s.scalars(select(ModelCallRecord).where(ModelCallRecord.run_id==p.run_id))).all()]
            nodes=[e.payload for e in (await s.scalars(select(RunEvent).where(RunEvent.run_id==p.run_id,RunEvent.event_type=='node_end').order_by(RunEvent.sequence))).all()]
        record={**p.record,'status':'interrupted','error_code':'EXECUTION_INTERRUPTED','nodes':nodes,'usage':usage_summary(calls)}
        await service.conversations.finish(p,record,[],None)
    async def consume(self,service,p):
        store=EventStore(self.db);request=ChatRequest(message=p.message,session_id=p.session_id)
        async with aclosing(service.events(request,p)) as stream:
            async for event in stream:
                # Finish already committed the terminal event with product history.
                if event['type'] not in {'complete','error'}:await store.emit(p,event)
    async def monitor(self,identifier,token):
        started=monotonic();heartbeat=started
        while True:
            await asyncio.sleep(.25)
            async with self.db.sessions.begin() as s:
                control=await s.scalar(select(RunControl).where(RunControl.run_id==identifier).with_for_update())
                if control.attempt_token!=token or control.status not in {'running','cancel_requested'}:return
                reason=None
                try:await actor(s,control)
                except ApplicationError:reason='RUN_AUTHORIZATION_CHANGED'
                from smart_cs.performance.policy import ServingPolicy
                serving=ServingPolicy.deployed()
                limit=min(self.max_seconds,serving.round_seconds) if serving.enabled else self.max_seconds
                if monotonic()-started>=limit:reason='RUN_TIME_LIMIT'
                if reason and control.status=='running':
                    control.status='cancel_requested';control.cancel_code=reason
                    await append(s,control,{'type':'cancellation_requested','run_id':identifier,'status':'cancel_requested'})
                if control.status=='cancel_requested':return
                if monotonic()-heartbeat>=5:
                    control.heartbeat_at=utcnow();control.lease_until=utcnow()+timedelta(seconds=30);heartbeat=monotonic()
