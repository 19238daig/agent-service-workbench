"""One measured model identity and text contract for document/query embeddings."""
import asyncio
from smart_cs.retrieval.clients import LocalModels
from smart_cs.schemas.errors import ApplicationError


class EmbeddingAdapter:
    def __init__(self,models=None):self.models=models or LocalModels()

    def identity(self):
        identity=self.models.embedding_identity()
        if identity.get('dimensions')!=1024:raise ApplicationError('EMBEDDING_DIMENSION_MISMATCH','当前索引只支持 1024 维向量，请为不同维度单独建表。',503)
        return {**identity,'provider':'ollama-local','normalization':'l2-f32-v1','distance':'cosine',
            'document_template':'title-heading-content-v1','query_prefix':''}

    async def aidentity(self):return await asyncio.to_thread(self.identity)

    async def documents(self,texts,expected):
        if await self.aidentity()!=expected:raise ApplicationError('EMBEDDING_MODEL_CHANGED','向量模型身份已变化，请重新建立索引。',503)
        vectors=await asyncio.to_thread(self.models.embed,texts)
        vectors=LocalModels.vectors(vectors,len(texts))
        if await self.aidentity()!=expected:raise ApplicationError('EMBEDDING_MODEL_CHANGED','构建期间模型发生变化，旧索引继续使用。',503)
        return vectors

    async def query(self,query,expected):
        if await self.aidentity()!=expected:raise ApplicationError('EMBEDDING_INDEX_IDENTITY_MISMATCH','查询模型与当前索引不匹配，请构建并切换兼容索引。',503)
        vectors=LocalModels.vectors(await self.models.aembed([query]),1)
        if await self.aidentity()!=expected:raise ApplicationError('EMBEDDING_MODEL_CHANGED','查询期间模型发生变化，请重试。',503)
        return vectors[0]
