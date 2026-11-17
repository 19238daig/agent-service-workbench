"""Owner-scoped public events, monotonically numbered inside SQL transactions."""
import json
from datetime import timezone
from sqlalchemy import select,func,text,update
from smart_cs.db.models import RunControl,RunEvent,ChatTurn,User,AuthCredential,AuthSession,ExecutionRun,ModelCallRecord,ActionRequest,utcnow
from smart_cs.schemas.errors import ApplicationError
from smart_cs.durable.context import run_attempt

ACTIVE={'queued','running','cancel_requested'}
MESSAGES={'RUN_CANCELLED':'已停止后续处理；已提交的业务结果仍然有效。',
    'EXECUTION_INTERRUPTED':'执行进程已中断，请查看已有结果后显式恢复；重连不会自动重做。',
    'RUN_AUTHORIZATION_CHANGED':'运行授权已变化，已停止后续处理。',
    'RUN_TIME_LIMIT':'本轮达到运行时限，请核实已有结果后再决定是否恢复。'}

def fail(code,message='运行不可用。',status=409):return ApplicationError(code,message,status)

async def actor(session,control,identity=None,*,lock=False,lock_session=False,read=False):
    turn=await session.get(ChatTurn,control.turn_id)
    query=select(User.role,AuthCredential.enabled).join(AuthCredential).where(User.id==control.user_id)
    if lock:query=query.with_for_update(read=True)
    row=(await session.execute(query)).one_or_none()
    if not turn or not row or not row.enabled or row.role!=turn.authorization_role or (identity and identity.role!=row.role):
        raise fail('RUN_AUTHORIZATION_CHANGED',MESSAGES['RUN_AUTHORIZATION_CHANGED'],403)
    auth_id=identity.auth_session_id if read and identity else control.auth_session_id
    if auth_id:
        auth=await session.scalar(select(AuthSession).where(AuthSession.id==auth_id).with_for_update(read=True)) if lock or lock_session else await session.get(AuthSession,auth_id)
        if not auth or auth.user_id!=control.user_id or auth.revoked_at or auth.refresh_expires_at.replace(tzinfo=timezone.utc)<=utcnow():
            raise fail('RUN_AUTHORIZATION_CHANGED',MESSAGES['RUN_AUTHORIZATION_CHANGED'],403)
    return turn

async def verify_lock(session):
    binding=run_attempt.get()
    if not binding or binding.get('lock_pid') is None:return
    if binding['checkpoint_connection'].closed:raise fail('EXECUTION_INTERRUPTED',MESSAGES['EXECUTION_INTERRUPTED'])
    key=binding['lock_key']&0xffffffffffffffff
    held=await session.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks WHERE locktype='advisory' AND pid=:pid AND classid=:hi AND objid=:lo AND objsubid=1 AND granted)"),{'pid':binding['lock_pid'],'hi':key>>32,'lo':key&0xffffffff})
    if not held:raise fail('EXECUTION_INTERRUPTED',MESSAGES['EXECUTION_INTERRUPTED'])

async def owned(session,run_id,identity,*,lock=False):
    query=select(RunControl).where(RunControl.run_id==run_id,RunControl.user_id==identity.subject_id)
    if lock:query=query.with_for_update()
    control=await session.scalar(query)
    if control is None:raise fail('RUN_NOT_FOUND','当前账号未找到此运行。',404)
    await actor(session,control,identity,read=True)
    return control

def public_event(payload):
    kind=payload.get('type')
    common={'type','request_id','trace_id','run_id','session_id'}
    fields={
        'queued':{'status','turn_id','persistence'},'running':{'status'},'cancellation_requested':{'status'},
        'session':{'turn_id','recording_status','persistence'},
        'node_start':{'node','model','elapsed_ms'},'node_end':{'node','model','duration_ms','elapsed_ms','status','summary'},
        'model_call_started':{'call_id','kind','model','agent_role','operation'},
        'model_call_finished':{'call_id','kind','status','duration_ms'},
        'error':{'code','message','error_type','http_status','duration_ms','action_execution','can_resume'},
    }
    if kind=='complete':
        from smart_cs.schemas.chat import ChatResponse
        result={'type':'complete',**ChatResponse.model_validate(payload).model_dump(mode='json')}
    elif kind in fields:result={key:value for key,value in payload.items() if key in common|fields[kind]}
    else:raise ValueError('Only public run events may be persisted')
    json.dumps(result,allow_nan=False)
    return result

async def append(session,control,payload):
    safe=public_event(payload)
    # SQL arithmetic also serializes the explicit SQLite adapter: graph callbacks
    # and progress projection can arrive concurrently in separate transactions.
    next_value=await session.scalar(update(RunControl).where(RunControl.run_id==control.run_id).values(
        next_event_sequence=RunControl.next_event_sequence+1,updated_at=utcnow()).returning(RunControl.next_event_sequence))
    sequence=next_value-1
    session.add(RunEvent(run_id=control.run_id,sequence=sequence,event_type=safe['type'],payload=safe))
    control.next_event_sequence=next_value
    return sequence

async def check_attempt(session,identity=None,*,allow_cancel=False):
    binding=run_attempt.get()
    if binding is None:return None
    query=select(RunControl).where(RunControl.run_id==binding['run_id']).with_for_update(read=True)
    control=await session.scalar(query)
    if control is None or control.attempt_token!=binding['token'] or control.status not in ({'running','cancel_requested'} if allow_cancel else {'running'}):
        raise fail('RUN_CANCELLED',MESSAGES['RUN_CANCELLED'])
    if identity and control.user_id!=identity.subject_id:raise fail('RUN_AUTHORIZATION_CHANGED',status=403)
    await verify_lock(session)
    await actor(session,control,identity,lock_session=True)
    return control

async def terminal(session,p,record,response):
    """Called BEFORE publishing history: terminal event and product facts commit together."""
    control=await session.scalar(select(RunControl).where(RunControl.run_id==p.run_id).with_for_update())
    if not control or control.attempt_token!=p.attempt_token or control.status not in ACTIVE:
        raise fail('RUN_SUPERSEDED','运行已结束或执行权已变化。')
    if control.status=='cancel_requested':
        record.update(status='cancelled',error_code=control.cancel_code or 'RUN_CANCELLED');response=None
    elif record['status']=='cancelled':
        record.update(status='interrupted',error_code='EXECUTION_INTERRUPTED');response=None
    if response is not None:
        try:
            await verify_lock(session)
            await actor(session,control,p.identity,lock=True)
        except ApplicationError as error:
            record.update(status='interrupted' if error.code=='EXECUTION_INTERRUPTED' else 'failed',error_code=error.code);response=None
    if response is not None:payload={'type':'complete',**response.model_dump(mode='json')}
    else:
        from smart_cs.services.execution import ActionExecutionService
        request=await session.scalar(select(ActionRequest).where(ActionRequest.turn_id==control.turn_id,ActionRequest.user_id==control.user_id))
        committed=await ActionExecutionService(None).receipt(session,request) if request else None
        payload=dict(p.terminal_event or {})
        code=record.get('error_code') or 'RUN_RECORDING_FAILED'
        payload.update(type='error',code=code,message=MESSAGES.get(code,payload.get('message','本轮未完成，请查询已有业务结果。')),
            run_id=p.run_id,session_id=p.session_id,request_id=p.ids.request_id,trace_id=p.ids.trace_id,
            action_execution=committed or {},can_resume=code!='RUN_AUTHORIZATION_CHANGED')
    started=await session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id==p.run_id,RunEvent.event_type=='model_call_started'))
    record['usage']={**record['usage'],'observed_started_calls':started,'recording_incomplete':record['status']=='interrupted'}
    if record['status']=='interrupted':record['usage']['status']='unknown'
    await append(session,control,payload)
    control.status=record['status'];control.lease_until=None
    return response

class EventStore:
    def __init__(self,database):self.db=database
    async def emit(self,p,payload):
        async with self.db.sessions.begin() as s:
            control=await s.scalar(select(RunControl).where(RunControl.run_id==p.run_id).with_for_update())
            if not control or control.attempt_token!=p.attempt_token or control.status!='running':raise fail('RUN_CANCELLED',MESSAGES['RUN_CANCELLED'])
            await actor(s,control,p.identity)
            await append(s,control,payload)
    async def model_call(self,phase,call):
        binding=run_attempt.get()
        if binding is None:return
        async with self.db.sessions.begin() as s:
            control=await s.scalar(select(RunControl).where(RunControl.run_id==binding['run_id']).with_for_update())
            if not control or control.attempt_token!=binding['token'] or control.status not in {'running','cancel_requested'}:raise fail('RUN_SUPERSEDED')
            if phase=='start':
                await verify_lock(s)
                await actor(s,control)
                if control.status!='running':raise fail('RUN_CANCELLED')
                await append(s,control,{'type':'model_call_started','run_id':control.run_id,'call_id':call['id'],**{k:call[k] for k in ['kind','model','agent_role','operation']}})
            else:
                from smart_cs.repositories.runs import DatabaseRunRepository
                await DatabaseRunRepository(self.db).calls_in_session(s,[call])
                await append(s,control,{'type':'model_call_finished','run_id':control.run_id,'call_id':call['id'],**{k:call[k] for k in ['kind','status','duration_ms']}})
