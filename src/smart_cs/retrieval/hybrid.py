"""B2: authorization/version filtering, exact dense + Chinese BM25, weighted RRF.

An application snapshot is rebuilt from the CURRENT authorized canonical chunks
of one active dense index. No result/corpus cache is shared between principals.
"""
import asyncio
from dataclasses import dataclass
import json
import os
from time import perf_counter
from pydantic import BaseModel,ConfigDict,Field,ValidationError
from sqlalchemy import text
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.retrieval.policy import COLLECTIONS,business_date
from smart_cs.retrieval.lexical import LexicalSnapshot,RESOURCES,fingerprint
from smart_cs.schemas.retrieval import RetrievalRequest
from smart_cs.schemas.errors import ApplicationError

PROTOCOL='hybrid-bm25-rrf-v1'
MAX_CHUNKS=50000
MAX_BYTES=32*1024*1024


class HybridConfig(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    k1:float=Field(default=1.5,gt=0,le=3)
    b:float=Field(default=.75,ge=0,le=1)
    dense_weight:float=Field(default=.5,ge=0,le=1)
    rrf_k:int=Field(default=60,ge=1,le=200)
    candidate_k:int=Field(default=20,ge=1,le=50)

    @classmethod
    def deployed(cls):return cls.model_validate_json((RESOURCES/'hybrid_profile.json').read_text(encoding='utf-8'))


def fuse(dense,lexical,config,limit):
    by_id={};scores={};ranks={}
    for name,documents,weight in [('dense',dense,config.dense_weight),('bm25',lexical,1-config.dense_weight)]:
        if weight==0:continue
        seen=set();rank=0
        for doc in documents:
            if doc['id'] in seen:continue
            seen.add(doc['id']);rank+=1;identifier=doc['id'];by_id[identifier]={**by_id.get(identifier,{}),**doc}
            scores[identifier]=scores.get(identifier,0)+weight/(config.rrf_k+rank);ranks.setdefault(identifier,{})[name]=rank
    ordered=sorted(scores,key=lambda identifier:(-scores[identifier],identifier))[:limit]
    return [{**by_id[i],'rrf_score':scores[i],'ranks':ranks[i]} for i in ordered]


@dataclass
class PreparedHybrid:
    index_id:str
    as_of:str
    embedding:str
    documents:list
    lexical:LexicalSnapshot
    timings:dict

    def rank(self,query,config,limit):
        sparse=self.lexical.search(query,config.candidate_k,config.k1,config.b)
        dense=self.documents[:config.candidate_k]
        return fuse(dense,sparse,config,limit),sparse


class HybridRetriever:
    def __init__(self,database,adapter=None,*,clock=business_date,config=None):
        self.database=database;self.clock=clock;self.dense=DenseRetriever(database,adapter,clock=clock);self.config=config or HybridConfig.deployed()

    async def prepare(self,query,*,identity=None,collections=COLLECTIONS):
        try:request=RetrievalRequest(query=query,top_k=self.config.candidate_k,collections=list(collections))
        except ValidationError:raise ApplicationError('INVALID_RETRIEVAL_QUERY','检索参数无效。',400) from None
        if self.database.engine.dialect.name!='postgresql':raise ApplicationError('PGVECTOR_REQUIRED','混合检索需要真实PostgreSQL向量索引。',503)
        started=perf_counter();day=self.clock()
        async with self.database.sessions() as s:
            index=await self.dense.active(s);identifier=index.id;expected=index.configuration['embedding_contract']
        vector=await self.dense.adapter.query(request.query,expected);embedded=perf_counter()
        async with self.database.sessions.begin() as s:
            await s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            for setting in ['enable_indexscan','enable_bitmapscan','enable_indexonlyscan']:await s.execute(text('SET LOCAL '+setting+'=off'))
            index=await self.dense.active(s)
            if index.id!=identifier:raise ApplicationError('INDEX_ACTIVE_CHANGED','查询期间索引已切换，请重试。',409)
            # Same predicates as B1. Filter BEFORE corpus statistics, ranks or LIMIT.
            statement=self.dense.statement(vector,identifier,identity,day,request.collections,None).limit(MAX_CHUNKS+1)
            rows=(await s.execute(statement)).all()
            if len(rows)>MAX_CHUNKS:raise ApplicationError('LEXICAL_CORPUS_LIMIT','当前可见语料超过应用侧混合索引容量。',503)
            documents=[];size=0
            for chunk,doc,manifest,receipt,distance in rows:
                self.dense.validate_row(chunk,doc,manifest,receipt);size+=len(chunk.content.encode())
                if size>MAX_BYTES:raise ApplicationError('LEXICAL_CORPUS_LIMIT','当前可见语料超过应用侧混合索引容量。',503)
                documents.append({**self.dense.serialize(chunk,doc,index,distance),'title':doc.title})
        fetched=perf_counter();lexical=await asyncio.to_thread(LexicalSnapshot,documents);built=perf_counter()
        return PreparedHybrid(identifier,day.isoformat(),expected['embedding'],documents,lexical,
            {'embedding_ms':(embedded-started)*1000,'database_ms':(fetched-embedded)*1000,'lexical_build_ms':(built-fetched)*1000,'prepare_ms':(built-started)*1000})

    async def search(self,query,top_k=20,*,identity=None,collections=COLLECTIONS):
        try:request=RetrievalRequest(query=query,top_k=top_k,collections=list(collections))
        except ValidationError:raise ApplicationError('INVALID_RETRIEVAL_QUERY','检索参数无效。',400) from None
        config=self.config.model_copy(update={'candidate_k':max(self.config.candidate_k,request.top_k)})
        prepared=await self.prepare(request.query,identity=identity,collections=collections);start=perf_counter()
        documents,sparse=await asyncio.to_thread(prepared.rank,request.query,config,top_k)
        await self.dense.revalidate(documents,identity)
        async with self.database.sessions() as s:active=await self.dense.active(s)
        if active.id!=prepared.index_id or self.clock().isoformat()!=prepared.as_of:raise ApplicationError('RETRIEVAL_CONTEXT_CHANGED','索引或业务日期已变化，请重试。',409)
        return {'index_id':prepared.index_id,'search':'hybrid_rrf','as_of':prepared.as_of,'embedding':prepared.embedding,'documents':documents,'evidence_only':True,
            'retrieval':{'protocol':PROTOCOL,'config':config.model_dump(),'config_sha256':fingerprint(config.model_dump()),
                'tokenizer':prepared.lexical.tokenizer.identity,'tokenizer_sha256':prepared.lexical.tokenizer.fingerprint,
                'lexical_snapshot_sha256':prepared.lexical.signature,'visible_chunks':len(prepared.documents),'sparse_candidates':len(sparse),
                'scores_are_confidence':False,'cross_principal_result_cache':False,
                'timings_ms':{**prepared.timings,'rank_and_revalidate_ms':(perf_counter()-start)*1000}}}


def configured_retriever(database,adapter=None,*,clock=business_date,mode=None):
    selected=mode or os.getenv('RAG_RETRIEVAL_MODE','dense')
    if selected=='dense':return DenseRetriever(database,adapter,clock=clock)
    if selected=='hybrid':return HybridRetriever(database,adapter,clock=clock)
    raise ApplicationError('RETRIEVAL_MODE_INVALID','RAG_RETRIEVAL_MODE必须为dense或hybrid。',503)


def pipeline_identity():
    if os.getenv('RAG_RETRIEVAL_MODE','dense')=='dense':return None
    if os.getenv('RAG_RETRIEVAL_MODE')!='hybrid':raise ApplicationError('RETRIEVAL_MODE_INVALID','检索模式无效。',503)
    from smart_cs.retrieval.lexical import tokenizer
    return {'protocol':PROTOCOL,'profile':HybridConfig.deployed().model_dump(),'tokenizer_sha256':tokenizer().fingerprint}
