"""Submit once; GET subscriptions never execute, resume or cancel a graph."""
import asyncio
import hashlib
import json
from contextlib import AsyncExitStack
from uuid import uuid4
from sqlalchemy import select
from smart_cs.db.models import RunControl,RunEvent,ChatTurn,ChatSession,ExecutionRun,utcnow
from smart_cs.schemas.chat import ChatRequest
from smart_cs.durable.store import owned,append,ACTIVE,fail,terminal

def request_hash(request,resume,expected_run_id=None):
    value={'message':'' if resume else request.message,'session_id':request.session_id,'kind':'resume' if resume else 'new','expected_run_id':expected_run_id}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def descriptor(control):
    return {'run_id':control.run_id,'session_id':control.session_id,'turn_id':control.turn_id,
        'status':control.status,'durability':'postgresql-run-events-v1','last_event_id':control.next_event_sequence-1,
        'events_url':f'/api/runs/{control.run_id}/events','can_subscribe':control.status in ACTIVE}

class RunService:
    def __init__(self,database,chat_service):self.db=database;self.chat=chat_service;self.runtime=chat_service.conversations
    async def submit(self,request,identity,*,resume=False,client_request_id=None,expected_run_id=None):
        if not identity.authenticated or self.runtime is None:raise fail('DURABLE_RUN_UNAVAILABLE','当前模式没有持久化执行器。',503)
        client_request_id=client_request_id or request.client_request_id or uuid4().hex
        sha=request_hash(request,resume,expected_run_id)
        # Serialize retries even when the first request created a new session ID.
        async with AsyncExitStack() as resources:
            await resources.enter_async_context(self.runtime.store.lock('run-submit:'+identity.subject_id+':'+client_request_id))
            async with self.db.sessions() as s:
                existing=await s.scalar(select(RunControl).where(RunControl.user_id==identity.subject_id,RunControl.client_request_id==client_request_id))
                if existing:
                    await owned(s,existing.run_id,identity)
                    if existing.request_hash!=sha:raise fail('RUN_IDEMPOTENCY_CONFLICT','同一请求标识不能更改消息、会话或操作。')
                    return descriptor(existing)
                if expected_run_id:
                    old=await owned(s,expected_run_id,identity)
                    turn=await s.get(ChatTurn,old.turn_id)
                    if turn.current_run_id!=expected_run_id:raise fail('RUN_NOT_CURRENT','此运行已被后续运行接管，请查询最新运行。')
            prepared=self.chat.prepare(request,identity);prepared.durable=True
            async def enqueue(s,p):
                if not identity.auth_session_id and self.db.engine.dialect.name=='postgresql' and not self.db.schema:raise fail('AUTHENTICATION_REQUIRED',status=401)
                control=RunControl(run_id=p.run_id,user_id=identity.subject_id,session_id=p.session_id,turn_id=p.turn_id,
                    auth_session_id=identity.auth_session_id,client_request_id=client_request_id,request_hash=sha,kind=p.kind,
                    status='queued',next_event_sequence=1)
                s.add(control);await s.flush()
                await append(s,control,{'type':'queued','run_id':p.run_id,'session_id':p.session_id,'turn_id':p.turn_id,
                    'request_id':p.ids.request_id,'trace_id':p.ids.trace_id,'status':'queued','persistence':self.runtime.store.backend})
                prepared.descriptor=descriptor(control)
            await self.runtime.prepare(request,prepared,resources,resume=resume,enqueue=enqueue)
            return prepared.descriptor
    async def detail(self,run_id,identity):
        async with self.db.sessions() as s:
            control=await owned(s,run_id,identity)
            result=descriptor(control)
            event=await s.scalar(select(RunEvent).where(RunEvent.run_id==run_id,RunEvent.event_type.in_(['complete','error'])).order_by(RunEvent.sequence.desc()).limit(1))
            return {**result,'result_event':event.payload if event else None,'cancellation_requested':control.status=='cancel_requested'}
    async def by_request(self,client_request_id,identity):
        async with self.db.sessions() as s:
            control=await s.scalar(select(RunControl).where(RunControl.user_id==identity.subject_id,RunControl.client_request_id==client_request_id))
            if control is None:raise fail('RUN_NOT_FOUND','未找到已提交的运行。',404)
        return await self.detail(control.run_id,identity)
    async def cancel(self,run_id,identity):
        async with self.db.sessions.begin() as s:
            control=await owned(s,run_id,identity,lock=True)
            if control.status not in ACTIVE:raise fail('RUN_ALREADY_TERMINAL','此运行已结束；已提交结果保留。')
            if control.status=='cancel_requested':return descriptor(control)
            if control.status=='queued':
                # No graph or model has started; publish cancellation atomically.
                await self._finish_queued(s,control)
            else:
                control.status='cancel_requested';control.cancel_code='RUN_CANCELLED'
                await append(s,control,{'type':'cancellation_requested','run_id':run_id,'status':'cancel_requested'})
            return descriptor(control)
    async def _finish_queued(self,s,control):
        from smart_cs.services.chat import PreparedRun
        from smart_cs.observability.context import RequestCorrelation
        run=await s.get(ExecutionRun,control.run_id);row=await s.scalar(select(ChatSession).where(ChatSession.id==control.session_id).with_for_update());turn=await s.get(ChatTurn,control.turn_id)
        control.attempt_token=uuid4().hex;control.status='cancel_requested';control.cancel_code='RUN_CANCELLED'
        p=PreparedRun(None,control.session_id,RequestCorrelation(run.request_id,run.trace_id,run.id),run.id,
            persistent=True,turn_id=turn.id,durable=True,attempt_token=control.attempt_token)
        record={**run.snapshot,'status':'cancelled','error_code':'RUN_CANCELLED'}
        await terminal(s,p,record,None)
        run.status=record['status'];run.snapshot=record;run.finished_at=utcnow()
        turn.status='cancelled';turn.assistant_message_id=None;turn.error_code='RUN_CANCELLED';turn.updated_at=utcnow()
        row.active_run_id=None;row.version+=1;row.updated_at=utcnow()
    async def page(self,run_id,identity,after):
        async with self.db.sessions() as s:
            control=await owned(s,run_id,identity)
            if after>=control.next_event_sequence:raise fail('RUN_CURSOR_AHEAD','事件游标超出此运行的范围。',400)
            events=(await s.scalars(select(RunEvent).where(RunEvent.run_id==run_id,RunEvent.sequence>after).order_by(RunEvent.sequence).limit(100))).all()
            return descriptor(control),[{'id':e.sequence,'event':e.event_type,'data':e.payload} for e in events]
    async def subscribe(self,run_id,identity,after=0):
        cursor=after;idle=0;terminal_sent=False
        while True:
            try:
                # StreamingResponse cancels its AnyIO scope on disconnect. Let a
                # short read finish/close its SQL connection before propagating it.
                read=asyncio.create_task(self.page(run_id,identity,cursor))
                try:state,events=await asyncio.shield(read)
                except asyncio.CancelledError:
                    import anyio
                    with anyio.CancelScope(shield=True):
                        try:await asyncio.shield(read)
                        except Exception:pass
                    raise
            except Exception:
                yield 'event: subscription_error\ndata: '+json.dumps({'type':'subscription_error','code':'SUBSCRIPTION_AUTHORIZATION_OR_STORAGE_CHANGED','message':'订阅不可用，请重新登录或查询运行状态。'})+'\n\n';return
            for event in events:
                cursor=event['id']
                if event['event'] in {'complete','error'}:terminal_sent=True
                yield f"id: {cursor}\nevent: {event['event']}\ndata: "+json.dumps(event['data'],ensure_ascii=False)+'\n\n'
            if state['status'] not in ACTIVE and cursor==state['last_event_id']:
                if not terminal_sent:yield 'event: stream_end\ndata: '+json.dumps({'type':'stream_end','status':state['status'],'last_event_id':cursor})+'\n\n'
                return
            if events:idle=0;continue
            await asyncio.sleep(.25);idle+=1
            if idle>=40:yield ': heartbeat\n\n';idle=0
