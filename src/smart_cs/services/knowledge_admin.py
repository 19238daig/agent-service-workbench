"""Administrative publication/withdrawal with immutable versions and atomic audit."""
import asyncio
import hashlib
from uuid import uuid4
from sqlalchemy import select,func
from smart_cs.db.models import User,AuthCredential,AuthSession,KnowledgeDocument,KnowledgeChunk,KnowledgeIngestion,RetrievalIndex,KnowledgeAdminEvent,utcnow
from smart_cs.ingestion.pipeline import IngestionPipeline,digest
from smart_cs.ingestion.errors import IngestionError
from smart_cs.services.approvals import aware
from smart_cs.retrieval.indexes import publication_lock,DenseIndexBuilder
from smart_cs.settings import PROJECT_ROOT
from smart_cs.schemas.errors import ApplicationError

def fail(code,message,status=409):return ApplicationError(code,message,status)
async def admin_actor(s,identity):
    if not identity.authenticated or identity.role!='admin':raise fail('FORBIDDEN','此操作需要管理员权限。',403)
    row=await s.scalar(select(User.id).join(AuthCredential).where(User.id==identity.subject_id,User.role=='admin',AuthCredential.enabled.is_(True)).with_for_update(read=True))
    if not row:raise fail('FORBIDDEN','管理员账号不可用。',403)
    if identity.auth_session_id:
        auth=await s.scalar(select(AuthSession).where(AuthSession.id==identity.auth_session_id).with_for_update(read=True))
        if not auth or auth.user_id!=identity.subject_id or auth.revoked_at or aware(auth.access_expires_at)<=utcnow() or aware(auth.refresh_expires_at)<=utcnow():raise fail('UNAUTHENTICATED','登录已失效。',401)

def document_view(d,chunks=0):
    meta=d.metadata_json or {}
    return {'id':d.id,'title':d.title,'source':d.source,'version':d.version,'status':d.status,'access_scope':d.access_scope,
        'owner_id':d.owner_id,'domain':d.domain,'content_hash':d.content_hash,'mime_type':d.mime_type,'chunks':chunks,
        'effective_from':d.effective_from,'effective_until':d.effective_until,'publisher':meta.get('publisher',''),
        'license':meta.get('license',''),'source_url':meta.get('source_url') or meta.get('url'),
        'source_key':meta.get('source_key',d.id),'file_url':f'/api/admin/knowledge/documents/{d.id}/file'}
def audit(s,identity,kind,resource,payload):
    s.add(KnowledgeAdminEvent(id=uuid4().hex,user_id=identity.subject_id,kind=kind,resource_id=resource,payload=payload))

class KnowledgeAdminService:
    def __init__(self,database,root=PROJECT_ROOT,adapter=None):self.db=database;self.root=root;self.adapter=adapter
    async def list(self,identity,limit=30,offset=0,status=None):
        async with self.db.sessions() as s:
            await admin_actor(s,identity)
            q=select(KnowledgeDocument)
            if status:q=q.where(KnowledgeDocument.status==status)
            total=await s.scalar(select(func.count()).select_from(q.subquery()))
            rows=(await s.scalars(q.outerjoin(KnowledgeIngestion,KnowledgeIngestion.document_id==KnowledgeDocument.id).order_by(KnowledgeIngestion.created_at.desc().nulls_last(),KnowledgeDocument.id).limit(limit).offset(offset))).all()
            counts=dict((await s.execute(select(KnowledgeChunk.document_id,func.count()).where(KnowledgeChunk.document_id.in_([d.id for d in rows])).group_by(KnowledgeChunk.document_id))).all()) if rows else {}
            return {'documents':[document_view(d,counts.get(d.id,0)) for d in rows],'total':total,'has_more':offset+len(rows)<total,'offset':offset}
    async def detail(self,identity,identifier):
        async with self.db.sessions() as s:
            await admin_actor(s,identity);d=await s.get(KnowledgeDocument,identifier)
            if not d:raise fail('DOCUMENT_NOT_FOUND','未找到文档。',404)
            count=await s.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.document_id==identifier))
            chunks=(await s.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id==identifier).order_by(KnowledgeChunk.ordinal).limit(3))).all()
            return {**document_view(d,count),'preview':[{'id':c.id,'content':c.content[:1500],'page':c.source_page,'heading':c.heading} for c in chunks]}
    async def snapshot_path(self,d):
        from pathlib import Path
        base=Path(self.root).resolve();path=(base/(d.file_path or '')).resolve()
        if not path.is_relative_to(base/'data') or path.suffix.lower() not in {'.pdf','.html','.htm','.md','.txt'} or not path.is_file():raise fail('DOCUMENT_FILE_UNAVAILABLE','原文件不可用。',404)
        if await asyncio.to_thread(lambda:hashlib.sha256(path.read_bytes()).hexdigest())!=d.content_hash:raise fail('SOURCE_ARTIFACT_CHANGED','原文件校验失败。')
        return path
    async def file(self,identity,identifier):
        async with self.db.sessions() as s:
            await admin_actor(s,identity);d=await s.get(KnowledgeDocument,identifier)
            if not d:raise fail('DOCUMENT_NOT_FOUND','未找到文档。',404)
            return await self.snapshot_path(d),d.source,d.mime_type
    async def change(self,identity,identifier,command,publish):
        async with self.db.sessions.begin() as s:
            await admin_actor(s,identity);await publication_lock(self.db,s)
            d=await s.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id==identifier).with_for_update())
            if not d:raise fail('DOCUMENT_NOT_FOUND','未找到文档。',404)
            if (d.content_hash,d.version)!=(command.content_hash,command.version):raise fail('DOCUMENT_BINDING_CHANGED','文档版本已变化，请刷新后重试。')
            before=d.status;target='published' if publish else 'withdrawn'
            if d.status==target:return {'document':document_view(d),'unchanged':True,'active_index_changed':False}
            if publish:
                receipt=await s.scalar(select(KnowledgeIngestion).where(KnowledgeIngestion.document_id==identifier))
                if not receipt:raise fail('DOCUMENT_NOT_MANAGED','仅支持发布入库流程生成的草稿。')
                if (receipt.artifact_sha256,receipt.version,receipt.acl_key)!=(d.content_hash,d.version,digest([d.access_scope,d.owner_id])):raise fail('INGESTION_BINDING_CHANGED','入库回执与资料版本或权限不一致。')
                await self.snapshot_path(d)
                try:await IngestionPipeline(self.db,root=self.root).publish_in_session(s,d)
                except IngestionError as error:raise fail(error.code,error.message) from None
            else:
                if d.status!='published':raise fail('DOCUMENT_NOT_PUBLISHED','只能撤回已发布文档。')
                d.status='withdrawn'
            audit(s,identity,'document_published' if publish else 'document_withdrawn',identifier,
                {'previous_status':before,'status':target,'version':d.version,'content_hash':d.content_hash})
            return {'document':document_view(d),'unchanged':False,'active_index_changed':False}
    async def indexes(self,identity):
        async with self.db.sessions() as s:
            await admin_actor(s,identity)
            rows=(await s.scalars(select(RetrievalIndex).order_by(RetrievalIndex.created_at.desc()).limit(30))).all()
            return {'active_index_id':await s.scalar(select(RetrievalIndex.id).where(RetrievalIndex.active.is_(True))),'indexes':[{'id':i.id,'active':i.active,
                'documents':i.configuration.get('documents'),'chunks':i.configuration.get('chunks'),'protocol':i.configuration.get('protocol'),
                'model_identity':i.model_identity,'created_at':i.created_at,'manifest_sha256':i.configuration.get('manifest_sha256')} for i in rows]}
    async def activate(self,identity,identifier,expected_active):
        async with self.db.sessions() as s:await admin_actor(s,identity)
        async def changed(s,index,previous):
            await admin_actor(s,identity)
            if previous and previous.id==index.id:return
            audit(s,identity,'index_activated',index.id,{'previous_index_id':previous.id if previous else None,'manifest_sha256':index.configuration['manifest_sha256']})
        return await DenseIndexBuilder(self.db,adapter=self.adapter,root=self.root).activate(identifier,expected_active=expected_active,after_change=changed)
    async def events(self,identity,limit=30):
        async with self.db.sessions() as s:
            await admin_actor(s,identity)
            return {'events':[{'id':e.id,'kind':e.kind,'resource_id':e.resource_id,'user_id':e.user_id,'payload':e.payload,'created_at':e.created_at} for e in (await s.scalars(select(KnowledgeAdminEvent).order_by(KnowledgeAdminEvent.created_at.desc()).limit(limit))).all()]}
