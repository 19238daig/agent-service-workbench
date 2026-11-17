"""Local operator import: immutable bytes -> preparation -> atomic document/chunks.

No embedding/index activation and no API/model write authority is granted here.
Source/version/ACL and artifact/version/ACL are independently unique in the DB.
"""
import hashlib
import json
from pathlib import Path
from sqlalchemy import select,text,func
from sqlalchemy.exc import IntegrityError
from smart_cs.db.models import KnowledgeDocument,KnowledgeChunk,KnowledgeIngestion,KnowledgeEmbedding,RetrievalIndex,User
from smart_cs.ingestion.preparation import prepare_document,PARSER_VERSION,CLEANER_VERSION,CHUNKER_VERSION
from smart_cs.ingestion.errors import IngestionError
from smart_cs.schemas.ingestion import DocumentSpec,IngestionOptions
from smart_cs.settings import PROJECT_ROOT

SUPPORTED={'.md':'text/markdown','.txt':'text/plain','.pdf':'application/pdf','.html':'text/html','.htm':'text/html'}
MAX_BYTES=25*1024*1024


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def spec_metadata(spec,**kwargs):
    result=spec.model_dump(mode='json',**kwargs)
    # Retain the exact step-eleven fingerprint for pre-existing metadata.
    if result.get('effective_until') is None:result.pop('effective_until',None)
    return result


class IngestionPipeline:
    def __init__(self,database=None,*,root=PROJECT_ROOT,models=None):
        self.database=database;self.root=Path(root).resolve();self.models=models
        self.storage=self.root/'data/knowledge-imports/blobs'

    def source(self,path,spec):
        path=Path(path).resolve();extension=path.suffix.lower()
        if extension not in SUPPORTED:raise IngestionError('UNSUPPORTED_FORMAT','支持 Markdown、TXT、文本 PDF、HTML。')
        if not path.is_file():raise IngestionError('SOURCE_NOT_FOUND','未找到输入文件。')
        with path.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
        if not raw or len(raw)>MAX_BYTES:raise IngestionError('SOURCE_SIZE_LIMIT','文件为空或超过 25 MiB。')
        sha=hashlib.sha256(raw).hexdigest()
        if spec.expected_sha256 and spec.expected_sha256!=sha:raise IngestionError('SOURCE_CHECKSUM_MISMATCH','原文件与声明的 SHA-256 不符。')
        return path,extension,raw,sha

    def snapshot(self,raw,sha,extension):
        target=(self.storage/sha/('source'+extension)).resolve()
        # Reject managed-directory symlinks/reparse points escaping the data root.
        if not target.is_relative_to((self.root/'data/knowledge-imports').resolve()) or not target.is_relative_to(self.root/'data'):
            raise IngestionError('UNSAFE_STORAGE_PATH','原文件快照路径不在受控数据目录中。')
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            import os,tempfile
            descriptor,name=tempfile.mkstemp(prefix='staged-',dir=target.parent)
            staged=Path(name).resolve()
            try:
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(raw);stream.flush();os.fsync(stream.fileno())
                try:os.link(staged,target)
                except FileExistsError:pass
            finally:
                if staged.is_relative_to(self.storage.resolve()):staged.unlink(missing_ok=True)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=sha:raise IngestionError('SNAPSHOT_INTEGRITY_FAILED','原文件快照已变化，停止入库。')
        return target

    def profile(self,spec,options,extension):
        identity=None
        if options.chunker=='bge-semantic':
            if self.models is None:
                from smart_cs.retrieval.clients import LocalModels
                self.models=LocalModels()
            identity=self.models.identity()['embedding']
        config={'parser':PARSER_VERSION,'cleaner':CLEANER_VERSION,
            'chunker':CHUNKER_VERSION if options.chunker=='structural' else 'bge-m3-adjacent-v1',
            'options':options.model_dump(),'embedding_model_for_chunking':identity,'extension':extension}
        return config,digest({'metadata':spec_metadata(spec,exclude={'source_key','expected_sha256'}),'configuration':config})

    def inspect(self,path,spec,options=None):
        """Synchronous dry-run; no database, model required only if explicitly selected."""
        spec=DocumentSpec.model_validate(spec);options=IngestionOptions.model_validate(options or {})
        original,extension,raw,sha=self.source(path,spec)
        config,profile=self.profile(spec,options,extension)
        # Dry-run parses the supplied bytes in a temporary directory, never the
        # mutable input path. It does not create managed publication artifacts.
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            snapshot=Path(folder)/('source'+extension);snapshot.write_bytes(raw)
            prepared=prepare_document(snapshot,options,self.models)
        return {'source':original.name,'artifact_sha256':sha,'bytes':len(raw),'configuration':config,
            'profile_fingerprint':profile,'metadata':spec.model_dump(mode='json'),**prepared}

    async def find_receipt(self,session,key,sha,spec,acl):
        result=await session.get(KnowledgeIngestion,key)
        if result is None:
            result=await session.scalar(select(KnowledgeIngestion).where(KnowledgeIngestion.artifact_sha256==sha,
                KnowledgeIngestion.version==spec.version,KnowledgeIngestion.acl_key==acl))
        return result

    async def existing(self,session,receipt,sha,profile,spec,publish):
        if receipt.artifact_sha256!=sha or receipt.profile_fingerprint!=profile:
            raise IngestionError('IMMUTABLE_VERSION_CONFLICT','同一来源、版本和权限的内容或配置已变化，请使用新版本。')
        document=await session.get(KnowledgeDocument,receipt.document_id)
        if (document.access_scope,document.owner_id,document.version,document.effective_until)!=(spec.access_scope,spec.owner_id,spec.version,spec.effective_until.isoformat() if spec.effective_until else None):
            raise IngestionError('INGESTION_BINDING_CHANGED','文档权限或版本已变化，不能通过重复导入覆盖。')
        if publish:await self.publish_in_session(session,document)
        count=await session.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.document_id==document.id))
        return {'document_id':document.id,'source_key':receipt.source_key,'version':document.version,'status':document.status,
            'unchanged':True,'duplicate_content':receipt.source_key!=spec.source_key,'chunks':count,'artifact_sha256':sha,
            'file_path':document.file_path,'embedding_status':await self.embedding_state(session,document.id,count),'active_index_changed':False}

    async def embedding_state(self,session,document_id,total=None):
        if total is None:total=await session.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.document_id==document_id))
        query=select(func.count()).select_from(KnowledgeEmbedding).join(KnowledgeChunk,KnowledgeChunk.id==KnowledgeEmbedding.chunk_id).join(
            RetrievalIndex,RetrievalIndex.id==KnowledgeEmbedding.index_id).where(KnowledgeChunk.document_id==document_id)
        active=await session.scalar(query.where(RetrievalIndex.active.is_(True)))
        if total and active==total:return 'ready'
        return 'indexed_inactive' if await session.scalar(query) else 'pending_index'

    async def publish_in_session(self,session,document):
        if document.status=='published':return
        if document.status!='draft':raise IngestionError('DOCUMENT_NOT_DRAFT','只允许发布草稿；重复导入不能恢复已撤回的文档。')
        from datetime import date
        if document.effective_from>date.today().isoformat() or document.effective_until and document.effective_until<=date.today().isoformat():raise IngestionError('DOCUMENT_NOT_EFFECTIVE','文档尚未生效或已经过期，请保留为草稿。')
        duplicate=await session.scalar(select(KnowledgeDocument.id).where(KnowledgeDocument.id!=document.id,
            KnowledgeDocument.status=='published',KnowledgeDocument.content_hash==document.content_hash,
            KnowledgeDocument.access_scope==document.access_scope,KnowledgeDocument.owner_id==document.owner_id).limit(1))
        if duplicate:raise IngestionError('ARTIFACT_ALREADY_PUBLISHED','相同原文件与权限范围已有已发布文档，请复用原文档。')
        document.status='published'

    async def lock(self,session):
        if self.database.engine.dialect.name=='postgresql':
            # A local operator batch is short; serialize all ingestion publication
            # transactions, including aliases and legacy artifact duplicate checks.
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await session.execute(text('SELECT pg_advisory_xact_lock(684992610715)'))

    async def ingest(self,path,spec,options=None,*,publish=False):
        try:return await self._ingest(path,spec,options,publish=publish)
        except IntegrityError:
            # SQLite test-adapter races and DB uniqueness are a final backstop.
            # The failed transaction has rolled back before checking the winner.
            spec=DocumentSpec.model_validate(spec);options=IngestionOptions.model_validate(options or {})
            _,extension,raw,sha=self.source(path,spec);_,profile=self.profile(spec,options,extension)
            acl=digest([spec.access_scope,spec.owner_id]);key=digest([spec.source_key,spec.version,acl])
            async with self.database.sessions.begin() as session:
                await self.lock(session)
                previous=await self.find_receipt(session,key,sha,spec,acl)
                if previous:return await self.existing(session,previous,sha,profile,spec,publish)
            raise

    async def _ingest(self,path,spec,options=None,*,publish=False):
        if self.database is None:raise IngestionError('DATABASE_REQUIRED','请先配置数据库并完成迁移。')
        spec=DocumentSpec.model_validate(spec);options=IngestionOptions.model_validate(options or {})
        original,extension,raw,sha=self.source(path,spec)
        config,profile=self.profile(spec,options,extension)
        acl=digest([spec.access_scope,spec.owner_id]);key=digest([spec.source_key,spec.version,acl])
        # Fast repeat does not re-parse PDFs or call the optional BGE chunker.
        async with self.database.sessions.begin() as session:
            await self.lock(session)
            previous=await self.find_receipt(session,key,sha,spec,acl)
            if previous:
                self.snapshot(raw,sha,extension)
                return await self.existing(session,previous,sha,profile,spec,publish)
        snapshot=self.snapshot(raw,sha,extension)
        import asyncio
        prepared=await asyncio.to_thread(prepare_document,snapshot,options,self.models)
        if hashlib.sha256(snapshot.read_bytes()).hexdigest()!=sha:raise IngestionError('SNAPSHOT_INTEGRITY_FAILED','解析期间原文件快照已变化。')
        identifier='ing-'+key[:48];chunk_set='ingest-'+profile[:32]
        async with self.database.sessions.begin() as session:
            await self.lock(session)
            previous=await self.find_receipt(session,key,sha,spec,acl)
            if previous:return await self.existing(session,previous,sha,profile,spec,publish)
            if spec.owner_id and await session.get(User,spec.owner_id) is None:raise IngestionError('UNKNOWN_OWNER','私有文档所属用户不存在。')
            document=KnowledgeDocument(id=identifier,source=original.name[:180],title=spec.title,version=spec.version,
                domain=spec.domain,status='draft',content=prepared['content'],content_hash=sha,effective_from=spec.effective_from.isoformat(),effective_until=spec.effective_until.isoformat() if spec.effective_until else None,
                source_type=spec.source_type,file_path=snapshot.relative_to(self.root).as_posix(),mime_type=SUPPORTED[extension],
                access_scope=spec.access_scope,owner_id=spec.owner_id,
                metadata_json={**spec_metadata(spec,exclude={'expected_sha256'}),'original_sha256':sha,'bytes':len(raw),
                    'original_filename':original.name,'normalized_content_sha256':hashlib.sha256(prepared['content'].encode()).hexdigest(),
                    'ingestion':{'profile_fingerprint':profile,'configuration':config,'stats':prepared['stats']}})
            session.add(document);await session.flush()
            for ordinal,chunk in enumerate(prepared['chunks']):
                session.add(KnowledgeChunk(id=identifier+'-'+str(ordinal),document_id=identifier,ordinal=ordinal,chunk_set=chunk_set,
                    content=chunk['content'],content_hash=chunk['content_hash'],source_page=chunk['page'],heading=chunk['heading'],
                    kind=chunk['kind'],anchor=chunk['anchor'],token_count=chunk['token_count'],embedding_status='pending',
                    locator={**chunk['locator'],'artifact_sha256':sha,'source_key':spec.source_key,'version':spec.version,'page':chunk['page'],'heading':chunk['heading'],'anchor':chunk['anchor']}))
            session.add(KnowledgeIngestion(id=key,document_id=identifier,source_key=spec.source_key,version=spec.version,
                acl_key=acl,artifact_sha256=sha,profile_fingerprint=profile))
            if publish:await self.publish_in_session(session,document)
            await session.flush()
        return {'document_id':identifier,'source_key':spec.source_key,'version':spec.version,'status':document.status,'unchanged':False,
            'chunks':len(prepared['chunks']),'stats':prepared['stats'],'artifact_sha256':sha,'file_path':document.file_path,
            'embedding_status':'pending_index','active_index_changed':False}

    async def publish(self,document_id):
        async with self.database.sessions.begin() as session:
            await self.lock(session)
            receipt=await session.scalar(select(KnowledgeIngestion).where(KnowledgeIngestion.document_id==document_id))
            if receipt is None:raise IngestionError('INGESTED_DOCUMENT_NOT_FOUND','仅支持发布此流程入库的文档。')
            document=await session.get(KnowledgeDocument,document_id)
            snapshot=(self.root/document.file_path).resolve()
            if not snapshot.is_relative_to((self.root/'data/knowledge-imports').resolve()) or hashlib.sha256(snapshot.read_bytes()).hexdigest()!=receipt.artifact_sha256:
                raise IngestionError('SNAPSHOT_INTEGRITY_FAILED','原文件快照校验失败。')
            await self.publish_in_session(session,document)
            return {'document_id':document_id,'status':document.status,'active_index_changed':False,'embedding_status':await self.embedding_state(session,document_id)}
