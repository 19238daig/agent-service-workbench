"""Administrator-only job submission, persistent status/events and cancellation."""
import asyncio
from pathlib import Path
from fastapi import APIRouter,Depends,Request,Query
from smart_cs.api.dependencies import administrator
from smart_cs.background.files import JobFiles,MAX_BYTES
from smart_cs.background.service import JobService
from smart_cs.schemas.jobs import JobRequest,JobRetry
from smart_cs.schemas.errors import ApplicationError

router=APIRouter(prefix='/api/admin',tags=['后台任务'])
def service(request):
    db=getattr(request.app.state,'database',None)
    if db is None:raise ApplicationError('DATABASE_REQUIRED','后台任务需要数据库。',503)
    return JobService(db)

@router.post('/job-artifacts',status_code=201,openapi_extra={'requestBody':{'required':True,'content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}}})
async def upload(request:Request,filename:str=Query(min_length=1,max_length=180),identity=Depends(administrator)):
    # Raw file body; no multipart parser, remote URL, arbitrary server path or parsing in API.
    extension=Path(filename).suffix.lower();chunks=[];size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>MAX_BYTES:raise ApplicationError('JOB_FILE_TOO_LARGE','文件超过25MiB。',413)
        chunks.append(chunk)
    service(request)
    return await asyncio.to_thread(JobFiles().store,b''.join(chunks),extension)

@router.post('/jobs',status_code=202)
async def submit(body:JobRequest,request:Request,identity=Depends(administrator)):return await service(request).submit(identity,body)
@router.get('/jobs')
async def listing(request:Request,identity=Depends(administrator)):return {'jobs':await service(request).listing(identity)}
@router.get('/jobs/{identifier}')
async def get(identifier:str,request:Request,identity=Depends(administrator)):return await service(request).get(identity,identifier)
@router.get('/jobs/{identifier}/events')
async def events(identifier:str,request:Request,after:int=Query(default=0,ge=0),identity=Depends(administrator)):return {'events':await service(request).events(identity,identifier,after)}
@router.post('/jobs/{identifier}/cancel')
async def cancel(identifier:str,request:Request,identity=Depends(administrator)):return await service(request).cancel(identity,identifier)
@router.post('/jobs/{identifier}/retry')
async def retry(identifier:str,body:JobRetry,request:Request,identity=Depends(administrator)):return await service(request).retry(identity,identifier,body.payload_hash)
