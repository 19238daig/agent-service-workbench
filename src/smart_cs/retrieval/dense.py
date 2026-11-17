"""Exact pgvector search: authorization/version filters precede ORDER BY/LIMIT."""
import hashlib
import math
from sqlalchemy import select,text
from pydantic import ValidationError
from smart_cs.db.models import KnowledgeDocument,KnowledgeChunk,KnowledgeEmbedding,RetrievalIndex,RetrievalIndexDocument,KnowledgeIngestion
from smart_cs.retrieval.embedding import EmbeddingAdapter
from smart_cs.retrieval.indexes import PROTOCOL,digest,document_state,chunk_state
from smart_cs.retrieval.policy import COLLECTIONS,business_date,current_document_ids
from smart_cs.schemas.errors import ApplicationError
from smart_cs.schemas.retrieval import RetrievalRequest


class DenseRetriever:
    def __init__(self,database,adapter=None,*,clock=business_date):
        self.database=database;self.adapter=adapter or EmbeddingAdapter();self.clock=clock

    async def active(self,session):
        index=await session.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
        if index is None:raise ApplicationError('ACTIVE_VECTOR_INDEX_MISSING','请先建立并切换完整向量索引。',503)
        if index.configuration.get('protocol')!=PROTOCOL:raise ApplicationError('INDEX_UPGRADE_REQUIRED','当前是旧索引，请用通用切片构建并切换新索引。',503)
        return index

    @staticmethod
    def statement(vector,index_id,identity,day,collections,top_k):
        distance=KnowledgeEmbedding.embedding.cosine_distance(vector)
        return select(KnowledgeChunk,KnowledgeDocument,RetrievalIndexDocument,KnowledgeIngestion,distance.label('distance')).join(
            KnowledgeDocument,KnowledgeDocument.id==KnowledgeChunk.document_id).join(KnowledgeEmbedding,KnowledgeEmbedding.chunk_id==KnowledgeChunk.id).join(
            RetrievalIndexDocument,(RetrievalIndexDocument.document_id==KnowledgeDocument.id)&(RetrievalIndexDocument.index_id==KnowledgeEmbedding.index_id)).outerjoin(
            KnowledgeIngestion,KnowledgeIngestion.document_id==KnowledgeDocument.id).where(
            KnowledgeEmbedding.index_id==index_id,KnowledgeDocument.id.in_(current_document_ids(identity,day)),KnowledgeDocument.domain.in_(collections),
            ~KnowledgeDocument.source_type.startswith('synthetic'),~KnowledgeDocument.source_type.startswith('test'),
            ~KnowledgeDocument.source_type.startswith('evaluation'),~KnowledgeDocument.source_type.startswith('dataset')
            ).order_by(distance,KnowledgeChunk.id).limit(top_k)

    @staticmethod
    def validate_row(chunk,document,manifest,receipt):
        if digest(document_state(document,receipt))!=manifest.document_fingerprint:
            raise ApplicationError('INDEX_DOCUMENT_STALE','资料内容或授权元数据已变化，请重建索引后重试。',503)
        if manifest.snapshot['chunks'].get(chunk.id)!=digest(chunk_state(chunk)) or hashlib.sha256(chunk.content.encode()).hexdigest()!=chunk.content_hash:
            raise ApplicationError('INDEX_CHUNK_STALE','切片内容或定位已变化，请重建索引。',503)

    @staticmethod
    def serialize(chunk,document,index,distance):
        if not math.isfinite(float(distance)):raise ApplicationError('INDEX_VECTOR_INVALID','索引向量无效，请核实并重建。',503)
        meta=document.metadata_json or {}
        return {'id':chunk.id,'content':chunk.content,'source':document.source,'score':1-float(distance),
            'metadata':{'document_id':document.id,'version':document.version,'page':chunk.source_page,'heading':chunk.heading,
                'artifact_sha256':document.content_hash,
                'anchor':chunk.anchor,'kind':chunk.kind,'locator':chunk.locator,'index_id':index.id,'collection':document.domain,
                'effective_from':document.effective_from,'effective_until':document.effective_until,
                'source_url':meta.get('source_url') or meta.get('url'),
                'url':f'/api/knowledge/documents/{document.id}/file'+(f'#page={chunk.source_page}' if chunk.source_page else '')},
            'retrieval_models':index.model_identity}

    async def search(self,query,top_k=20,*,identity=None,collections=COLLECTIONS):
        try:request=RetrievalRequest(query=query,top_k=top_k,collections=list(collections))
        except ValidationError:raise ApplicationError('INVALID_RETRIEVAL_QUERY','检索问题不能为空或超过 2000 字符，top_k 为 1 至 50。',400) from None
        if self.database.engine.dialect.name!='postgresql':raise ApplicationError('PGVECTOR_REQUIRED','真实向量检索需要 PostgreSQL；离线测试不伪造向量排名。',503)
        day=self.clock()
        async with self.database.sessions() as session:
            index=await self.active(session);expected=index.configuration['embedding_contract'];identifier=index.id
        vector=await self.adapter.query(request.query,expected)
        async with self.database.sessions.begin() as session:
            await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            await session.execute(text('SET LOCAL enable_indexscan=off'))
            await session.execute(text('SET LOCAL enable_bitmapscan=off'))
            await session.execute(text('SET LOCAL enable_indexonlyscan=off'))
            index=await self.active(session)
            if index.id!=identifier:raise ApplicationError('INDEX_ACTIVE_CHANGED','查询期间索引已切换，请重试。',409)
            rows=(await session.execute(self.statement(vector,identifier,identity,day,request.collections,request.top_k))).all()
            result=[]
            for chunk,document,manifest,receipt,distance in rows:
                self.validate_row(chunk,document,manifest,receipt);result.append(self.serialize(chunk,document,index,distance))
        return {'index_id':identifier,'search':'exact_cosine','as_of':day.isoformat(),'embedding':expected['embedding'],'documents':result,'evidence_only':True}

    async def revalidate(self,documents,identity=None):
        if not documents:return
        ids=[d['id'] for d in documents]
        async with self.database.sessions() as session:
            index=await self.active(session)
            rows=(await session.execute(select(KnowledgeChunk,KnowledgeDocument,RetrievalIndexDocument,KnowledgeIngestion).join(
                KnowledgeDocument,KnowledgeDocument.id==KnowledgeChunk.document_id).join(RetrievalIndexDocument,
                (RetrievalIndexDocument.document_id==KnowledgeDocument.id)&(RetrievalIndexDocument.index_id==index.id)).outerjoin(
                KnowledgeIngestion,KnowledgeIngestion.document_id==KnowledgeDocument.id).where(KnowledgeChunk.id.in_(ids),
                KnowledgeDocument.id.in_(current_document_ids(identity,self.clock()))))).all()
            allowed={c.id:(c,d,m,r) for c,d,m,r in rows}
            for evidence in documents:
                item=allowed.get(evidence['id'])
                if item is None or evidence['metadata'].get('index_id')!=index.id or item[0].content!=evidence['content'] or item[1].id!=evidence['metadata'].get('document_id'):
                    raise ApplicationError('RETRIEVAL_AUTHORIZATION_CHANGED','资料授权、版本或索引已变化，请重新检索。',409)
                self.validate_row(*item)

    async def revalidate_citations(self,citations,identity=None):
        """Resolve server-rendered quote anchors again at the final release boundary."""
        if not citations:return
        async with self.database.sessions() as session:
            index=await self.active(session)
            rows=(await session.execute(select(KnowledgeChunk,KnowledgeDocument,RetrievalIndexDocument,KnowledgeIngestion).join(
                KnowledgeDocument,KnowledgeDocument.id==KnowledgeChunk.document_id).join(RetrievalIndexDocument,
                (RetrievalIndexDocument.document_id==KnowledgeDocument.id)&(RetrievalIndexDocument.index_id==index.id)).outerjoin(
                KnowledgeIngestion,KnowledgeIngestion.document_id==KnowledgeDocument.id).where(
                KnowledgeChunk.id.in_([c['chunk_id'] for c in citations]),KnowledgeDocument.id.in_(current_document_ids(identity,self.clock()))))).all()
            by_id={c.id:(c,d,m,r) for c,d,m,r in rows}
            for citation in citations:
                item=by_id.get(citation['chunk_id'])
                if item is None:raise ApplicationError('RETRIEVAL_AUTHORIZATION_CHANGED','引用资料已不可用，请重新检索。',409)
                chunk,document,manifest,receipt=item
                self.validate_row(*item)
                expected=self.serialize(chunk,document,index,0)['metadata']
                if any(citation.get(k)!=expected.get(k) for k in ['document_id','version','page','heading','url','source_url','collection','index_id','artifact_sha256','locator','effective_from','effective_until']) or (
                    citation.get('source')!=document.source or citation.get('chunk_sha256')!=chunk.content_hash):
                    raise ApplicationError('CITATION_BINDING_CHANGED','引用版本、切片或定位已变化，请重新检索。',409)
                quotes=citation.get('quotes')
                if not isinstance(quotes,list) or not quotes:raise ApplicationError('CITATION_QUOTE_INVALID','引用缺少原文锚点。',409)
                for quote in quotes:
                    start,end=quote.get('start'),quote.get('end')
                    if type(start) is not int or type(end) is not int or not 0<=start<end<=len(chunk.content) or chunk.content[start:end]!=quote.get('text'):
                        raise ApplicationError('CITATION_QUOTE_INVALID','引用原文与切片不符。',409)
