"""Signed Redis rank IDs only; reconstruct evidence from current authorized SQL rows."""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import time

import httpx
from sqlalchemy import select
from smart_cs.settings import PROJECT_ROOT
from smart_cs.db.models import KnowledgeDocument,KnowledgeChunk,RetrievalIndexDocument,KnowledgeIngestion
from smart_cs.retrieval.policy import current_document_ids,COLLECTIONS
from smart_cs.retrieval.indexes import digest,document_state
from smart_cs.retrieval.reranking import CrossEncoderReranker,RerankConfig
from smart_cs.services.access import principal_context
from smart_cs.performance.policy import ServingPolicy
from smart_cs.performance.runtime import event
from smart_cs.observability.context import run_context


class RedisRanks:
    async def read(self,key):
        from redis.asyncio import Redis
        client=Redis.from_url(os.environ['REDIS_URL'],socket_timeout=.3,socket_connect_timeout=.3)
        try:return await client.get(key)
        finally:await client.aclose()

    async def write(self,key,value,ttl,limit):
        from redis.asyncio import Redis
        client=Redis.from_url(os.environ['REDIS_URL'],socket_timeout=.3,socket_connect_timeout=.3)
        index=key.rsplit(':',1)[0]+':entries'
        script='''redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]); redis.call('ZADD',KEYS[2],ARGV[3],KEYS[1]);
        redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',ARGV[4]);
        while redis.call('ZCARD',KEYS[2])>tonumber(ARGV[5]) do
          local removed=redis.call('ZPOPMIN',KEYS[2],1); redis.call('DEL',removed[1]);
        end; redis.call('EXPIRE',KEYS[2],tonumber(ARGV[2])+60); return 1'''
        try:await client.eval(script,2,key,index,value,ttl,time.time(),time.time()-ttl,limit)
        finally:await client.aclose()


def signing_key():
    path=PROJECT_ROOT/'.local/performance/cache-signing.key';path.parent.mkdir(parents=True,exist_ok=True)
    try:
        descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'wb') as stream:stream.write(secrets.token_bytes(32))
    except FileExistsError:pass
    value=path.read_bytes()
    if len(value)!=32:raise ValueError('Invalid cache signing key')
    return value


class EvidenceCache:
    def __init__(self,database,dense,*,policy=None,backend=None,secret=None):
        self.database=database;self.dense=dense;self.policy=policy or ServingPolicy.deployed()
        self.backend=backend or RedisRanks();self.secret=secret
        namespace=os.getenv('PERF_CACHE_NAMESPACE','serving-v1')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',namespace):raise ValueError('Invalid cache namespace')
        self.prefix='smartcs:perf:'+namespace
        self.enabled=self.policy.enabled and self.policy.cache_enabled and (backend is not None or (
            bool(os.getenv('REDIS_URL')) and not database.schema and os.getenv('SMART_CS_WORKER_CONTAINER')!='1'))

    async def scope(self):
        actor=principal_context.get();day=self.dense.clock()
        async with self.database.sessions() as s:
            index=await self.dense.active(s)
            statement=select(KnowledgeDocument,RetrievalIndexDocument,KnowledgeIngestion).join(RetrievalIndexDocument,
                (RetrievalIndexDocument.document_id==KnowledgeDocument.id)&(RetrievalIndexDocument.index_id==index.id)).outerjoin(
                KnowledgeIngestion,KnowledgeIngestion.document_id==KnowledgeDocument.id).where(
                KnowledgeDocument.id.in_(current_document_ids(actor,day)),KnowledgeDocument.domain.in_(COLLECTIONS),
                ~KnowledgeDocument.source_type.startswith('synthetic'),~KnowledgeDocument.source_type.startswith('test'),
                ~KnowledgeDocument.source_type.startswith('evaluation'),~KnowledgeDocument.source_type.startswith('dataset')).order_by(KnowledgeDocument.id)
            visible=(await s.execute(statement)).all()
        value={'principal':actor.model_dump() if actor else {'scope':'public_anonymous'},'date':day.isoformat(),
               'index':index.id,'configuration':index.configuration,'model':index.model_identity,
               'documents':[(d.id,digest(document_state(d,r)),m.document_fingerprint,m.chunk_fingerprint) for d,m,r in visible]}
        return digest(value),index

    async def hydrate(self,values,index):
        actor=principal_context.get();ids=[v['id'] for v in values]
        if len(ids)>50 or len(set(ids))!=len(ids):raise ValueError('Invalid cached ranks')
        async with self.database.sessions() as s:
            rows=(await s.execute(select(KnowledgeChunk,KnowledgeDocument,RetrievalIndexDocument,KnowledgeIngestion).join(
                KnowledgeDocument,KnowledgeDocument.id==KnowledgeChunk.document_id).join(RetrievalIndexDocument,
                (RetrievalIndexDocument.document_id==KnowledgeDocument.id)&(RetrievalIndexDocument.index_id==index.id)).outerjoin(
                KnowledgeIngestion,KnowledgeIngestion.document_id==KnowledgeDocument.id).where(
                KnowledgeChunk.id.in_(ids),KnowledgeDocument.id.in_(current_document_ids(actor,self.dense.clock()))))).all()
        by_id={c.id:(c,d,m,r) for c,d,m,r in rows};documents=[]
        for value in values:
            item=by_id.get(value['id'])
            if item is None:raise ValueError('Cached scope no longer readable')
            self.dense.validate_row(*item)
            score=value['score']
            if type(score) not in {int,float} or not math.isfinite(score) or not -1.00001<=score<=1.00001:raise ValueError('Invalid cosine')
            doc=self.dense.serialize(item[0],item[1],index,1-score)
            for key in ['rerank_score','bm25_score','rrf_score']:
                if key in value:
                    if type(value[key]) not in {float,int} or not math.isfinite(value[key]):raise ValueError('Invalid score')
                    doc[key]=value[key]
            if 'ranks' in value:doc['ranks']=value['ranks']
            documents.append(doc)
        await self.dense.revalidate(documents)
        return documents

    async def get_or_load(self,stage,query,configuration,loader,*,candidates=None):
        if not self.enabled:return await loader()
        observation=run_context.get();scope,index=await self.scope()
        signature=digest({'stage':stage,'query':query,'configuration':configuration,'scope':scope,
                          'candidates':[(d['id'],hashlib.sha256(d['content'].encode()).hexdigest()) for d in candidates] if candidates is not None else None})
        try:
            secret=self.secret or signing_key()
            key=self.prefix+':'+hmac.new(secret,signature.encode(),hashlib.sha256).hexdigest()
            raw=await self.backend.read(key)
            if raw:
                if len(raw)>32768:raise ValueError('Cache payload exceeds limit')
                envelope=json.loads(raw);body=envelope['body']
                expected=hmac.new(secret,json.dumps(body,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected,envelope['mac']) or body['binding']!=signature:raise ValueError('Cache signature invalid')
                if await self.dense.adapter.aidentity()!=index.configuration['embedding_contract']:raise ValueError('Embedding identity changed')
                if stage=='rerank' and configuration['enabled']:
                    async with httpx.AsyncClient(trust_env=False,timeout=5) as client:
                        await CrossEncoderReranker(RerankConfig.model_validate(configuration)).identity(client)
                documents=await self.hydrate(body['rows'],index)
                if candidates is not None and not {d['id'] for d in documents}<={d['id'] for d in candidates}:raise ValueError('Candidate mismatch')
                if (await self.scope())[0]!=scope:raise ValueError('Cache context changed')
                event(observation,'cache_hit',stage=stage);return documents
        except Exception:
            event(observation,'cache_error',stage=stage)
        event(observation,'cache_miss',stage=stage)
        documents=await loader()
        await self.dense.revalidate(documents)
        if (await self.scope())[0]!=scope:return documents
        try:
            body={'binding':signature,'rows':[{k:d[k] for k in ['id','score','rerank_score','rrf_score','bm25_score','ranks'] if k in d} for d in documents]}
            mac=hmac.new(secret,json.dumps(body,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
            await self.backend.write(key,json.dumps({'body':body,'mac':mac},separators=(',',':')).encode(),self.policy.cache_ttl_seconds,self.policy.cache_max_entries)
        except Exception:event(observation,'cache_error',stage=stage)
        return documents
