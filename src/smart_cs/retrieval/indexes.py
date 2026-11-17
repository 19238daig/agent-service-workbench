"""Versioned dense indexes built from canonical chunks, never copied demo vectors."""
import hashlib
import json
import os
from datetime import date,timezone
from pathlib import Path
from sqlalchemy import select,update,func,or_,text
from smart_cs.db.models import KnowledgeDocument,KnowledgeChunk,KnowledgeIngestion,KnowledgeEmbedding,RetrievalIndex,RetrievalIndexDocument
from smart_cs.retrieval.embedding import EmbeddingAdapter
from smart_cs.retrieval.policy import COLLECTIONS
from smart_cs.schemas.errors import ApplicationError
from smart_cs.settings import PROJECT_ROOT

PROTOCOL='dense-exact-v1'


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def stamp(value):
    if value is None:return None
    return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.astimezone(timezone.utc).isoformat()


def document_state(document,receipt):
    fields={key:getattr(document,key) for key in ['id','title','version','domain','status','content_hash','effective_from',
        'effective_until','source_type','file_path','mime_type','access_scope','owner_id','source','metadata_json']}
    fields['normalized_content_sha256']=hashlib.sha256(document.content.encode()).hexdigest()
    fields['receipt']=None if receipt is None else {'source_key':receipt.source_key,'version':receipt.version,
        'acl_key':receipt.acl_key,'artifact_sha256':receipt.artifact_sha256,'profile_fingerprint':receipt.profile_fingerprint,'created_at':stamp(receipt.created_at)}
    return fields


def chunk_state(chunk):
    return {key:getattr(chunk,key) for key in ['id','ordinal','chunk_set','content_hash','source_page','heading','kind','anchor','token_count','locator']}


def manifest_record(document,receipt,chunks):
    state=document_state(document,receipt)
    signatures={c.id:digest(chunk_state(c)) for c in chunks}
    return {'document_id':document.id,'chunk_set':chunks[0].chunk_set,'chunk_count':len(chunks),
        'document_fingerprint':digest(state),'chunk_fingerprint':digest(signatures),
        'snapshot':{'document':state,'chunks':signatures}}


async def publication_lock(database,session):
    if database.engine.dialect.name=='postgresql':
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await session.execute(text('SELECT pg_advisory_xact_lock(684992610715)'))


class DenseIndexBuilder:
    def __init__(self,database,adapter=None,*,root=PROJECT_ROOT):
        self.database=database;self.adapter=adapter or EmbeddingAdapter();self.root=Path(root).resolve()

    async def collect(self,session):
        active=await session.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
        rows=(await session.execute(select(KnowledgeDocument,KnowledgeIngestion).outerjoin(KnowledgeIngestion,
            KnowledgeIngestion.document_id==KnowledgeDocument.id).where(KnowledgeDocument.status=='published',
            KnowledgeDocument.domain.in_(COLLECTIONS)).order_by(KnowledgeDocument.id))).all()
        result=[]
        for document,receipt in rows:
            meta=document.metadata_json or {}
            if document.source_type.startswith(('synthetic','test','evaluation','dataset')):continue
            if receipt is None and not (document.id.startswith('file-') and meta.get('original_sha256')==document.content_hash):continue
            try:
                start=date.fromisoformat(document.effective_from)
                end=date.fromisoformat(document.effective_until) if document.effective_until else None
                if end and end<=start:raise ValueError('Invalid interval')
            except ValueError:raise ApplicationError('DOCUMENT_VALIDITY_INVALID','文档日期无效，请核实来源版本后重建。',409) from None
            if not document.file_path:raise ApplicationError('SOURCE_ARTIFACT_MISSING','文档缺少原文件快照。',409)
            path=(self.root/document.file_path).resolve()
            if not path.is_relative_to(self.root/'data') or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=document.content_hash:
                raise ApplicationError('SOURCE_ARTIFACT_CHANGED','原文件缺失或哈希发生变化，未修改当前索引。',409)
            if receipt:
                if receipt.acl_key!=digest([document.access_scope,document.owner_id]) or receipt.version!=document.version or receipt.artifact_sha256!=document.content_hash:
                    raise ApplicationError('INGESTION_BINDING_CHANGED','入库回执与文档的权限、版本或原文件不匹配。',409)
                expected_set='ingest-'+receipt.profile_fingerprint[:32]
            else:
                query=select(KnowledgeChunk.chunk_set).join(KnowledgeEmbedding,KnowledgeEmbedding.chunk_id==KnowledgeChunk.id).join(
                    RetrievalIndex,RetrievalIndex.id==KnowledgeEmbedding.index_id).where(KnowledgeChunk.document_id==document.id).order_by(
                    RetrievalIndex.active.desc(),RetrievalIndex.created_at.desc(),RetrievalIndex.id.desc()).limit(1)
                expected_set=await session.scalar(query)
                if expected_set is None:raise ApplicationError('LEGACY_CHUNK_SET_MISSING','旧原生文档缺少可确认的切片集，请重新入库。',409)
            chunks=list((await session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id==document.id,
                KnowledgeChunk.chunk_set==expected_set).order_by(KnowledgeChunk.ordinal,KnowledgeChunk.id))).all())
            if not chunks:raise ApplicationError('DOCUMENT_CHUNKS_MISSING','已发布文档没有完整切片，请先完成入库。',409)
            expected_count=meta.get('ingestion',{}).get('stats',{}).get('chunks') if receipt else None
            if expected_count is not None and len(chunks)!=expected_count:raise ApplicationError('DOCUMENT_CHUNKS_INCOMPLETE','入库切片数量与回执不一致。',409)
            if [c.ordinal for c in chunks]!=list(range(len(chunks))):raise ApplicationError('DOCUMENT_CHUNKS_INCOMPLETE','切片序号不完整，未切换索引。',409)
            for chunk in chunks:
                if not chunk.content.strip() or hashlib.sha256(chunk.content.encode()).hexdigest()!=chunk.content_hash:
                    raise ApplicationError('CHUNK_CONTENT_CHANGED','切片正文与哈希不匹配，请重新入库。',409)
            result.append((document,receipt,chunks,manifest_record(document,receipt,chunks)))
        if not result:raise ApplicationError('EMPTY_INDEX_CORPUS','没有已发布、带原文件和合法切片的业务资料。',409)
        return result,active

    def records(self,rows):return [r[3] for r in rows]

    def frozen_public_source(self,rows):
        # Keep old evaluation provenance only if its entire PUBLIC source set is
        # unchanged. Private documents do not enter public evaluation retrieval.
        public=[d for d,_,_,_ in rows if d.access_scope=='public']
        path=(self.root/os.getenv('RAG_SOURCE_DIRECTORY','data/public-after-sales-v2/sources')).resolve().parent/'source_manifest.json'
        if not path.is_relative_to(self.root/'data') or not path.is_file():return None,None
        try:
            raw=path.read_bytes();manifest=json.loads(raw)
            if {(d.source,d.content_hash) for d in public}!={(s['file'],s['sha256']) for s in manifest['sources']}:return None,None
            return manifest['id'],hashlib.sha256(raw).hexdigest()
        except (ValueError,KeyError,TypeError):return None,None

    async def build(self,*,activate=False,progress=None):
        identity=await self.adapter.aidentity()
        async with self.database.sessions() as session:rows,previous=await self.collect(session)
        records=self.records(rows);corpus,source_hash=self.frozen_public_source(rows)
        configuration={'protocol':PROTOCOL,'search':'exact_cosine','chunker':'canonical-structural-or-bge',
            'corpus':corpus or 'published-native','source_manifest_hash':source_hash,'collections':list(COLLECTIONS),
            'manifest_sha256':digest(records),'documents':len(records),'chunks':sum(r['chunk_count'] for r in records),
            'embedding_contract':identity,'version_policy':'latest-started-visible-acl-family-v1'}
        identifier='dense-v1-'+digest([identity,configuration])[:40]
        async with self.database.sessions() as session:existing=await session.get(RetrievalIndex,identifier)
        unchanged=existing is not None
        if not unchanged:
            texts=[d.title+'\n'+(c.heading or '')+'\n'+c.content for d,_,chunks,_ in rows for c in chunks]
            if progress is None:vectors=await self.adapter.documents(texts,identity)
            else:
                vectors=[]
                for start in range(0,len(texts),8):
                    await progress('embedding',start,len(texts))
                    vectors.extend(await self.adapter.documents(texts[start:start+8],identity))
                await progress('committing_index',len(texts),len(texts))
            async with self.database.sessions.begin() as session:
                await publication_lock(self.database,session)
                current,_=await self.collect(session)
                if digest(self.records(current))!=digest(records):raise ApplicationError('INDEX_CORPUS_CHANGED','构建期间资料或权限发生变化，请重新构建；旧索引保持使用。',409)
                if await self.adapter.aidentity()!=identity:raise ApplicationError('EMBEDDING_MODEL_CHANGED','构建期间向量模型发生变化，请重新构建。',503)
                existing=await session.get(RetrievalIndex,identifier)
                if existing is None:
                    index=RetrievalIndex(id=identifier,active=False,model_identity={**identity,'reranker':'bge-reranker-v2-m3','device':'not_probed'},configuration=configuration)
                    session.add(index)
                    await session.flush()
                    await session.execute(RetrievalIndexDocument.__table__.insert(),[{'index_id':identifier,**record} for record in records])
                    flattened=[c for _,_,chunks,_ in rows for c in chunks]
                    payload=[{'index_id':identifier,'chunk_id':chunk.id,'embedding':vector} for chunk,vector in zip(flattened,vectors)]
                    if len(payload)!=configuration['chunks'] or len({p['chunk_id'] for p in payload})!=configuration['chunks']:
                        raise ApplicationError('INDEX_VECTOR_ALIGNMENT_FAILED','切片与向量数量不一致，构建未提交。',409)
                    # Keep a concrete complete batch, rather than individual ORM
                    # objects. Check the stored membership BEFORE committing.
                    # Acknowledge every key. This is the correctness baseline for
                    # local CLI builds; batching can be optimized separately.
                    for item in payload:
                        written=await session.scalar(KnowledgeEmbedding.__table__.insert().values(**item).returning(KnowledgeEmbedding.chunk_id))
                        if written!=item['chunk_id']:raise ApplicationError('INDEX_VECTOR_WRITE_FAILED','向量写入回执不完整，构建未提交。',503)
                    await self.validate_vectors(session,index)
                else:unchanged=True
        if activate:await self.activate(identifier,expected_active=previous.id if previous else None)
        async with self.database.sessions() as session:
            index=await session.get(RetrievalIndex,identifier)
            await self.validate_vectors(session,index)
        return {'index_id':identifier,'unchanged':unchanged,'active':index.active,'previous_index_id':previous.id if previous else None,
            'documents':configuration['documents'],'chunks':configuration['chunks'],'embedding_identity':identity,
            'manifest_sha256':configuration['manifest_sha256'],'search':'exact_cosine','model_calls':'local_embedding_only'}

    async def validate_vectors(self,session,index):
        if index.configuration.get('protocol')!=PROTOCOL:raise ApplicationError('INDEX_PROTOCOL_UNSUPPORTED','请使用通用切片建立受校验索引。',409)
        if self.database.engine.dialect.name=='postgresql':
            await session.execute(text('SET LOCAL enable_indexscan=off'))
            await session.execute(text('SET LOCAL enable_bitmapscan=off'))
            await session.execute(text('SET LOCAL enable_indexonlyscan=off'))
        count=await session.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index.id))
        manifests=list((await session.scalars(select(RetrievalIndexDocument).where(RetrievalIndexDocument.index_id==index.id).order_by(RetrievalIndexDocument.document_id))).all())
        if not manifests or count!=index.configuration['chunks'] or sum(m.chunk_count for m in manifests)!=count or len(manifests)!=index.configuration['documents']:
            raise ApplicationError('INDEX_INCOMPLETE',f"索引未提交或切换：向量 {count}/{index.configuration['chunks']}，文档清单 {len(manifests)}/{index.configuration['documents']}，清单切片 {sum(m.chunk_count for m in manifests)}。",409)
        records=[{key:getattr(m,key) for key in ['document_id','chunk_set','chunk_count','document_fingerprint','chunk_fingerprint','snapshot']} for m in manifests]
        actual=set((await session.scalars(select(KnowledgeEmbedding.chunk_id).where(KnowledgeEmbedding.index_id==index.id))).all())
        expected={key for m in manifests for key in m.snapshot['chunks']}
        if actual!=expected or any(m.chunk_count!=len(m.snapshot['chunks']) for m in manifests) or digest(records)!=index.configuration['manifest_sha256']:
            raise ApplicationError('INDEX_MANIFEST_MISMATCH','索引成员与不可变清单不一致，未切换。',409)
        if self.database.engine.dialect.name=='postgresql':
            norm=func.vector_norm(KnowledgeEmbedding.embedding)
            invalid=await session.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index.id,
                or_(func.vector_dims(KnowledgeEmbedding.embedding)!=1024,norm<=0,func.abs(norm-1)>0.0001)))
            if invalid:raise ApplicationError('INDEX_VECTOR_INVALID','索引向量维度或归一化校验失败。',409)
        return manifests

    async def activate(self,identifier,*,expected_active,after_change=None):
        identity=await self.adapter.aidentity()
        async with self.database.sessions.begin() as session:
            await publication_lock(self.database,session)
            index=await session.get(RetrievalIndex,identifier)
            if index is None:raise ApplicationError('INDEX_NOT_FOUND','未找到待切换索引。',404)
            if index.configuration.get('embedding_contract')!=identity:raise ApplicationError('EMBEDDING_INDEX_IDENTITY_MISMATCH','待切换索引与实际模型不兼容，未切换。',409)
            current=await session.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
            if (current.id if current else None)!=expected_active:raise ApplicationError('INDEX_ACTIVE_CHANGED','其他操作者已切换索引，请核实当前版本。',409)
            await self.validate_vectors(session,index)
            rows,_=await self.collect(session)
            if digest(self.records(rows))!=index.configuration['manifest_sha256']:raise ApplicationError('INDEX_CORPUS_CHANGED','资料或权限已变化，不能切换到过期的索引。',409)
            await session.execute(update(RetrievalIndex).where(RetrievalIndex.active.is_(True)).values(active=False))
            await session.execute(update(RetrievalIndex).where(RetrievalIndex.id==identifier).values(active=True))
            ids=[c.id for _,_,chunks,_ in rows for c in chunks]
            await session.execute(update(KnowledgeChunk).where(KnowledgeChunk.id.in_(ids)).values(embedding_status='ready'))
            if after_change is not None:await after_change(session,index,current)
        return {'index_id':identifier,'active':True,'previous_index_id':expected_active,'search':'exact_cosine'}
