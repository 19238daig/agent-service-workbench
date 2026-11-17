"""B2 deterministic algebra and real-pgvector authorization/version boundaries."""
import asyncio
from datetime import date
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from uuid import uuid4
from sqlalchemy import select,delete
from smart_cs.db.database import Database
from smart_cs.db.models import User,KnowledgeDocument,KnowledgeChunk,KnowledgeIngestion,KnowledgeEmbedding,RetrievalIndex,RetrievalIndexDocument
from smart_cs.ingestion.pipeline import IngestionPipeline
from smart_cs.schemas.ingestion import DocumentSpec
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.retrieval.lexical import tokenizer,LexicalSnapshot
from smart_cs.retrieval.hybrid import HybridConfig,HybridRetriever,fuse,configured_retriever
from smart_cs.retrieval.embedding import EmbeddingAdapter
from smart_cs.retrieval.indexes import DenseIndexBuilder
from smart_cs.services.access import principal_context
from test_dense_retrieval import StubModels


def document(identifier,content):return {'id':identifier,'content':content,'score':.8,'title':'参考资料','metadata':{'heading':'规则'}}


class HybridAlgorithmTests(unittest.TestCase):
    def test_chinese_identifiers_nfkc_and_negative_words(self):
        t=tokenizer();values=t.tokenize('不退款 ORDER-A-001 ＳＥＮＤ ＥＭＡＩＬ')
        self.assertIn('不',values);self.assertIn('退款',values);self.assertIn('order-a-001',values);self.assertIn('send',values)
        self.assertEqual(t.identity['jieba'],'0.42.1');self.assertEqual(len(t.fingerprint),64)

    def test_sparse_single_document_still_has_positive_idf(self):
        snapshot=LexicalSnapshot([document('one','七日无理由退货要求')]);found=snapshot.search('无理由退货',20)
        self.assertEqual(len(found),1);self.assertGreater(found[0]['bm25_score'],0)

    def test_no_overlap_and_stopwords_never_get_sparse_ranks(self):
        snapshot=LexicalSnapshot([document('one','退款规则'),document('two','知识库管理')])
        self.assertEqual(snapshot.search('xyznomatch',20),[]);self.assertEqual(snapshot.search('the a 的',20),[])

    def test_rrf_uses_unique_one_based_ranks_and_keeps_scores_separate(self):
        a,b,c=[document(k,k) for k in ['a','b','c']];config=HybridConfig()
        found=fuse([a,a,b],[b,c],config,20)
        self.assertEqual(found[0]['id'],'b');self.assertEqual(found[0]['ranks'],{'dense':2,'bm25':1})
        self.assertAlmostEqual(found[0]['rrf_score'],.5/62+.5/61);self.assertEqual(found[0]['score'],.8)

    def test_empty_sparse_and_weight_endpoints_are_deterministic(self):
        a,b=[document(k,k) for k in ['z','a']]
        self.assertEqual([d['id'] for d in fuse([a,b],[],HybridConfig(),20)],['z','a'])
        self.assertEqual([d['id'] for d in fuse([a,b],[b],HybridConfig(dense_weight=1.),20)],['z','a'])
        self.assertEqual([d['id'] for d in fuse([a,b],[b],HybridConfig(dense_weight=0.),20)],['a'])

    def test_configuration_and_modes_do_not_accept_authority_fields(self):
        from pydantic import ValidationError
        from smart_cs.schemas.retrieval import RetrievalRequest
        for fields in [{'rrf_k':True},{'dense_weight':float('nan')},{'b':2.},{'candidate_k':51},{'actor':'admin'}]:
            with self.assertRaises(ValidationError):HybridConfig.model_validate(fields)
        with self.assertRaises(ValidationError):RetrievalRequest(query='退款',user_id='bob')
        with self.assertRaises(ApplicationError):configured_retriever(None,mode='unknown')


class HybridDatabaseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.url='sqlite+aiosqlite:///'+(cls.root/'test.db').as_posix();cls.schema=None
        if os.getenv('AUTH_VERIFY_SCHEMA'):cls.url=os.environ['DATABASE_URL'];cls.schema=os.environ['AUTH_VERIFY_SCHEMA']
        async def setup():
            db=Database(cls.url,schema=cls.schema)
            try:
                await db.initialize(create_schema=True)
                async with db.sessions.begin() as s:s.add_all([User(id='alice',display_name='Alice'),User(id='bob',display_name='Bob'),User(id='staff',display_name='Staff',role='support'),User(id='admin',display_name='Admin',role='admin')])
            finally:await db.close()
        asyncio.run(setup())

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    async def asyncSetUp(self):
        self.db=Database(self.url,schema=self.schema);self.workspace=self.root/uuid4().hex;self.workspace.mkdir();self.number=0
        if self.db.engine.dialect.name!='postgresql':self.skipTest('Real pgvector and repeatable-read snapshot required')
        async with self.db.sessions.begin() as s:
            for model in [KnowledgeEmbedding,RetrievalIndexDocument,RetrievalIndex,KnowledgeChunk,KnowledgeIngestion,KnowledgeDocument]:await s.execute(delete(model))
        self.models=StubModels();self.adapter=EmbeddingAdapter(self.models);self.builder=DenseIndexBuilder(self.db,self.adapter,root=self.workspace)
        self.hybrid=HybridRetriever(self.db,self.adapter,clock=lambda:date(2026,11,14))
        self.alice=IdentityContext(subject_id='alice',authenticated=True,mode='authenticated_session');self.bob=self.alice.model_copy(update={'subject_id':'bob'})

    async def asyncTearDown(self):await self.db.close()

    async def add(self,body='退款规则',scope='public',owner=None,family=None,start='2020-01-01',end=None,status='published',source_type='business_document',domain='consumer_reference'):
        self.number+=1;p=self.workspace/f'source-{self.number}.txt';p.write_text(body+f' fixture {self.number}',encoding='utf-8')
        spec=DocumentSpec(source_key=family or 'source-'+str(self.number),title='服务参考',version='v'+str(self.number),domain=domain,access_scope=scope,owner_id=owner,effective_from=start,effective_until=end,source_type=source_type,publisher='Test fixture',license='test-only')
        result=await IngestionPipeline(self.db,root=self.workspace).ingest(p,spec)
        async with self.db.sessions.begin() as s:(await s.get(KnowledgeDocument,result['document_id'])).status=status
        return result['document_id']

    async def test_authorization_precedes_sparse_statistics_and_ranks(self):
        public=await self.add();await self.builder.build(activate=True);before=await self.hybrid.search('退款规则')
        secret=await self.add('BOB_ONLY 稀有词 ' * 50,scope='private',owner='bob');own=await self.add('ALICE_ONLY',scope='private',owner='alice');await self.builder.build(activate=True)
        anonymous=await self.hybrid.search('退款规则');self.assertEqual(anonymous['retrieval']['visible_chunks'],1)
        self.assertEqual(anonymous['documents'][0]['id'],before['documents'][0]['id']);self.assertEqual(anonymous['documents'][0]['bm25_score'],before['documents'][0]['bm25_score'])
        alice=await self.hybrid.search('BOB_ONLY',identity=self.alice);bob=await self.hybrid.search('BOB_ONLY',identity=self.bob)
        self.assertNotIn(secret,{d['metadata']['document_id'] for d in alice['documents']});self.assertIn(secret,{d['metadata']['document_id'] for d in bob['documents']})
        self.assertIn(own,{d['metadata']['document_id'] for d in alice['documents']});self.assertFalse(alice['retrieval']['cross_principal_result_cache'])

    async def test_principal_context_propagates_through_memory_style_call(self):
        await self.add();secret=await self.add('本人私有证据',scope='private',owner='alice');await self.builder.build(activate=True)
        token=principal_context.set(self.alice)
        try:result=await self.hybrid.search('本人私有证据')
        finally:principal_context.reset(token)
        self.assertIn(secret,{d['metadata']['document_id'] for d in result['documents']})
        anonymous=await self.hybrid.search('本人私有证据');self.assertNotIn(secret,{d['metadata']['document_id'] for d in anonymous['documents']})

    async def test_scope_date_type_and_unindexed_documents_do_not_enter_corpus(self):
        good=await self.add();await self.add('未来',start='2027-01-01');await self.add('过期',end='2021-01-01');await self.add('草稿',status='draft');await self.add('评测',source_type='evaluation_case');await self.add('客服可见',scope='support');await self.add('管理可见',scope='admin');await self.add('客服手册',domain='helpdesk_reference')
        await self.builder.build(activate=True);result=await self.hybrid.search('退款规则',collections=['consumer_reference'])
        self.assertEqual({d['metadata']['document_id'] for d in result['documents']},{good});self.assertEqual(result['retrieval']['visible_chunks'],1)

    async def test_withdrawn_latest_revision_does_not_revive_old_version(self):
        await self.add('基础资料');old=await self.add('旧版条款',family='policy');await self.add('撤回新版',family='policy',start='2026-01-01',status='withdrawn')
        await self.builder.build(activate=True);result=await self.hybrid.search('旧版条款')
        self.assertNotIn(old,{d['metadata']['document_id'] for d in result['documents']})

    async def test_hidden_tampering_does_not_influence_public_query_but_owner_fails_closed(self):
        await self.add();secret=await self.add('隐私条款',scope='private',owner='bob');await self.builder.build(activate=True)
        async with self.db.sessions.begin() as s:(await s.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id==secret))).content='TAMPERED'
        result=await self.hybrid.search('退款规则');self.assertEqual(result['retrieval']['visible_chunks'],1)
        with self.assertRaisesRegex(ApplicationError,'INDEX_CHUNK_STALE'):await self.hybrid.search('隐私条款',identity=self.bob)

    async def test_revocation_after_snapshot_is_revalidated_before_return(self):
        identifier=await self.add();await self.builder.build(activate=True);prepared=await self.hybrid.prepare('退款规则')
        async with self.db.sessions.begin() as s:(await s.get(KnowledgeDocument,identifier)).status='withdrawn'
        with patch.object(self.hybrid,'prepare',new=AsyncMock(return_value=prepared)):
            with self.assertRaisesRegex(ApplicationError,'RETRIEVAL_AUTHORIZATION_CHANGED'):await self.hybrid.search('退款规则')

    async def test_index_or_model_change_during_query_fails_closed(self):
        await self.add();await self.builder.build(activate=True);self.models.change_on_query=True
        with self.assertRaisesRegex(ApplicationError,'EMBEDDING_MODEL_CHANGED'):await self.hybrid.search('退款规则')
        self.models.change_on_query=False;self.models.name='stub@'+'a'*64
        original=self.adapter.query
        async def switch(query,expected):
            vector=await original(query,expected);await self.add('新增条款');await self.builder.build(activate=True);return vector
        with patch.object(self.adapter,'query',new=switch):
            with self.assertRaisesRegex(ApplicationError,'INDEX_ACTIVE_CHANGED'):await self.hybrid.search('退款规则')

    async def test_no_visible_corpus_is_empty_and_top50_remains_compatible(self):
        await self.add('仅本人',scope='private',owner='bob');await self.builder.build(activate=True)
        result=await self.hybrid.search('仅本人',50);self.assertEqual(result['documents'],[]);self.assertEqual(result['retrieval']['visible_chunks'],0)
        result=await self.hybrid.search('仅本人',50,identity=self.bob);self.assertEqual(len(result['documents']),1)

    async def test_pipeline_switch_invalidates_task_context_and_dense_rolls_back_exactly(self):
        from smart_cs.persistence.conversations import context_fingerprint
        async with self.db.sessions() as s:
            with patch.dict(os.environ,{'RAG_RETRIEVAL_MODE':'dense'}):before=await context_fingerprint(s,self.alice)
            with patch.dict(os.environ,{'RAG_RETRIEVAL_MODE':'hybrid'}):changed=await context_fingerprint(s,self.alice)
            with patch.dict(os.environ,{'RAG_RETRIEVAL_MODE':'dense'}):after=await context_fingerprint(s,self.alice)
        self.assertNotEqual(before,changed);self.assertEqual(before,after)
