from fastapi import APIRouter,Depends,Request,Query
from fastapi.responses import FileResponse
from typing import Literal
from smart_cs.api.dependencies import administrator
from smart_cs.schemas.knowledge_admin import DocumentCommand,ActivateIndex
from smart_cs.services.knowledge_admin import KnowledgeAdminService
from smart_cs.schemas.errors import ApplicationError

router=APIRouter(prefix='/api/admin/knowledge',tags=['知识管理'])
def service(request):
    db=getattr(request.app.state,'database',None)
    if db is None:raise ApplicationError('DATABASE_REQUIRED','知识管理需要数据库。',503)
    return KnowledgeAdminService(db)
@router.get('/documents')
async def listing(request:Request,limit:int=Query(30,ge=1,le=100),offset:int=Query(0,ge=0),status:Literal['draft','published','withdrawn']|None=None,identity=Depends(administrator)):
    return await service(request).list(identity,limit,offset,status)
@router.get('/documents/{identifier}')
async def detail(identifier:str,request:Request,identity=Depends(administrator)):return await service(request).detail(identity,identifier)
@router.get('/documents/{identifier}/file')
async def file(identifier:str,request:Request,identity=Depends(administrator)):
    path,source,mime=await service(request).file(identity,identifier)
    return FileResponse(path,media_type='application/pdf' if mime=='application/pdf' else 'text/plain; charset=utf-8',
        headers={'Content-Security-Policy':'sandbox','X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})
@router.post('/documents/{identifier}/publish')
async def publish(identifier:str,body:DocumentCommand,request:Request,identity=Depends(administrator)):return await service(request).change(identity,identifier,body,True)
@router.post('/documents/{identifier}/withdraw')
async def withdraw(identifier:str,body:DocumentCommand,request:Request,identity=Depends(administrator)):return await service(request).change(identity,identifier,body,False)
@router.get('/indexes')
async def indexes(request:Request,identity=Depends(administrator)):return await service(request).indexes(identity)
@router.post('/indexes/{identifier}/activate')
async def activate(identifier:str,body:ActivateIndex,request:Request,identity=Depends(administrator)):return await service(request).activate(identity,identifier,body.expected_active_index_id)
@router.get('/events')
async def events(request:Request,identity=Depends(administrator)):return await service(request).events(identity)
