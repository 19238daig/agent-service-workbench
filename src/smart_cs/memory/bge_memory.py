"""Verified exact dense retrieval plus the existing local cross-encoder stage."""
import os
from sqlalchemy import select,func
from smart_cs.db.models import KnowledgeEmbedding,RetrievalIndex,RetrievalIndexDocument
from smart_cs.retrieval.clients import LocalModels
from smart_cs.retrieval.embedding import EmbeddingAdapter
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.retrieval.reranking import RerankConfig,CrossEncoderReranker
from smart_cs.retrieval.policy import business_date
from smart_cs.schemas.errors import ApplicationError


class BGEKnowledgeMemory:
    retrieval_mode='local_bge_m3_pgvector'
    local_rag=True
    def __init__(self,database,*,clock=business_date):
        self.database=database;self.models=LocalModels();self.mode=os.getenv('RAG_RETRIEVAL_MODE','dense')
        self.rerank_config=RerankConfig.deployed()
        self.clock=clock
        self.candidate_k=self.rerank_config.candidate_k;self.context_k=self.rerank_config.context_k
        if self.mode not in {'dense','hybrid'}:raise ApplicationError('RETRIEVAL_MODE_INVALID','检索模式无效。',503)
        self.retrieval_mode='local_bge_m3_bm25_rrf' if self.mode=='hybrid' else 'local_bge_m3_pgvector'
    def retriever(self):return DenseRetriever(self.database,EmbeddingAdapter(self.models),clock=self.clock)

    async def validate_runtime(self):
        result=await self.describe()
        if await EmbeddingAdapter(self.models).aidentity()!=result['embedding_contract']:
            raise ApplicationError('EMBEDDING_INDEX_IDENTITY_MISMATCH','实际向量模型与当前索引不匹配。',503)
        return result

    async def describe(self):
        async with self.database.sessions() as session:
            index=await self.retriever().active(session)
            count=await session.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index.id))
            docs=await session.scalar(select(func.count()).select_from(RetrievalIndexDocument).where(RetrievalIndexDocument.index_id==index.id))
            if count!=index.configuration['chunks'] or docs!=index.configuration['documents']:
                raise ApplicationError('INDEX_INCOMPLETE','当前索引不完整，请核实索引清单。',503)
            return {'index_id':index.id,'models':index.model_identity,'chunks':count,'documents':docs,
                'chunker':index.configuration['chunker'],'search':'exact_cosine','embedding_contract':index.configuration['embedding_contract']}

    async def search(self,query,top_k=20):
        from smart_cs.retrieval.hybrid import configured_retriever
        from smart_cs.performance.cache import EvidenceCache
        from smart_cs.retrieval.hybrid import HybridConfig
        from smart_cs.retrieval.lexical import tokenizer
        pipeline={'profile':HybridConfig.deployed().model_dump(),'tokenizer':tokenizer().fingerprint} if self.mode=='hybrid' else None
        async def load():return (await configured_retriever(self.database,EmbeddingAdapter(self.models),mode=self.mode,clock=self.clock).search(query,top_k))['documents']
        return await EvidenceCache(self.database,self.retriever()).get_or_load('search',query,{'mode':self.mode,'top_k':top_k,'pipeline':pipeline},load)

    async def revalidate(self,documents):
        await self.retriever().revalidate(documents)

    async def rerank(self,query,documents,top_k=4):
        config=self.rerank_config.model_copy(update={'context_k':min(top_k,self.context_k)})
        from smart_cs.performance.cache import EvidenceCache
        async def load():return (await CrossEncoderReranker(config).rank(query,documents,revalidate=self.revalidate))['documents']
        return await EvidenceCache(self.database,self.retriever()).get_or_load('rerank',query,config.model_dump(),load,candidates=documents)
