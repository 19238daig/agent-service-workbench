"""Run commands and GET-only, owner-scoped SSE replay."""
from fastapi import APIRouter,Depends,Request,Query,Header
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from smart_cs.api.dependencies import current_identity
from smart_cs.schemas.durable import RunSubmission,RunResume,RunCancellation
from smart_cs.schemas.chat import ChatRequest
from smart_cs.durable.store import owned,fail
from smart_cs.db.models import ChatTurn

router=APIRouter(tags=['持久化运行'])
def service(request:Request):
    factory=getattr(request.app.state,'run_service_factory',None)
    if factory is None:raise fail('DURABLE_RUN_UNAVAILABLE','持久化运行尚未就绪。',503)
    return factory()
def ready(request):
    factory=getattr(request.app.state,'require_workflow',None)
    if factory:factory()

@router.post('/api/runs',status_code=202)
async def create(body:RunSubmission,request:Request,identity=Depends(current_identity)):
    ready(request)
    return await service(request).submit(ChatRequest(**body.model_dump()),identity)

@router.get('/api/runs/by-request/{client_request_id}')
async def by_request(client_request_id:str,request:Request,identity=Depends(current_identity)):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{16,100}',client_request_id):raise fail('INVALID_CLIENT_REQUEST_ID','请求标识格式无效。',400)
    return await service(request).by_request(client_request_id,identity)

@router.post('/api/sessions/{session_id}/runs/resume',status_code=202)
async def resume_session(session_id:str,body:RunResume,request:Request,identity=Depends(current_identity)):
    ready(request)
    return await service(request).submit(ChatRequest(message='resume',session_id=session_id),identity,resume=True,client_request_id=body.client_request_id)

@router.post('/api/runs/{run_id}/resume',status_code=202)
async def resume_run(run_id:str,body:RunResume,request:Request,identity=Depends(current_identity)):
    ready(request);runs=service(request)
    async with runs.db.sessions() as s:control=await owned(s,run_id,identity)
    return await runs.submit(ChatRequest(message='resume',session_id=control.session_id),identity,resume=True,client_request_id=body.client_request_id,expected_run_id=run_id)

@router.post('/api/runs/{run_id}/cancel')
async def cancel(run_id:str,body:RunCancellation,request:Request,identity=Depends(current_identity)):
    return await service(request).cancel(run_id,identity)

@router.get('/api/runs/{run_id}/events')
async def events(run_id:str,request:Request,after:int|None=Query(default=None,ge=0,le=2147483647),last_event_id:str|None=Header(default=None,alias='Last-Event-ID'),identity=Depends(current_identity)):
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdigit() or len(last_event_id)>10 or int(last_event_id)>2147483647:raise fail('INVALID_RUN_CURSOR','Last-Event-ID 必须是非负整数。',400)
        cursor=int(last_event_id)
        if after is not None and after!=cursor:raise fail('CONFLICTING_RUN_CURSOR','请求头与查询参数的事件游标不一致。',400)
    else:cursor=after or 0
    runs=service(request);await runs.page(run_id,identity,cursor)
    return StreamingResponse(runs.subscribe(run_id,identity,cursor),media_type='text/event-stream',
        headers={'Cache-Control':'no-store','X-Accel-Buffering':'no','X-Run-ID':run_id})
