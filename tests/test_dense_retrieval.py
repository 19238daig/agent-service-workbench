"""Dense lifecycle/policy tests. Ranking fixtures are stubs, never quality scores.

Run the same suite on real pgvector with scripts/verify_dense_retrieval.py.
"""
import asyncio
from datetime import date
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from sqlalchemy import select,delete,update,func
from smart_cs.db.database import Database
from smart_cs.db.models import User,KnowledgeDocument,KnowledgeChunk,KnowledgeIngestion,KnowledgeEmbedding,RetrievalIndex,RetrievalIndexDocument
from smart_cs.ingestion.pipeline import IngestionPipeline
from smart_cs.schemas.ingestion import DocumentSpec
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.retrieval.embedding import EmbeddingAdapter
from smart_cs.retrieval.indexes import DenseIndexBuilder
from smart_cs.retrieval.dense import DenseRetriever
from smart_cs.retrieval.policy import current_document_ids


class StubModels:
    """Explicit deterministic SQL ranking fixture, not a semantic model."""
    def __init__(self):self.name='stub@'+'a'*64;self.dimensions=1024;self.inputs=[];self.invalid=None;self.change_on_query=False
    def embedding_identity(self):return {'embedding':self.name,'dimensions':self.dimensions}
    def embed(self,texts):
        self.inputs.extend(texts)
        if self.invalid=='raise':raise RuntimeError('simulated upstream failure')
        if self.invalid=='zero':return [[0.]*1024 for _ in texts]
        if self.invalid=='dimension':return [[1.]*768 for _ in texts]
        return [([1.,0.] if 'HIGH' in t else [0.6,0.8])+[0.]*1022 for t in texts]
    async def aembed(self,texts):
        if self.change_on_query:self.name='stub@'+'b'*64
        return [[1.,0.]+[0.]*1022 for _ in texts]


class DenseRetrievalTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cls.url='sqlite+aiosqlite:///'+(cls.root/'dense.db').as_posix();cls.schema=None
        if os.getenv('AUTH_VERIFY_SCHEMA'):cls.url=os.environ['DATABASE_URL'];cls.schema=os.environ['AUTH_VERIFY_SCHEMA']
        async def setup():
            db=Database(cls.url,schema=cls.schema)
            try:
                await db.initialize(create_schema=True)
                async with db.sessions.begin() as s:
                    s.add_all([User(id='alice',display_name='Alice'),User(id='bob',display_name='Bob'),User(id='staff',display_name='Staff',role='support')])
            finally:await db.close()
        asyncio.run(setup())

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    async def asyncSetUp(self):
        self.db=Database(self.url,schema=self.schema)
        # Delete only explicit tables in the disposable DB/schema. The PG runner
        # grants this role no public-table write permissions.
        async with self.db.sessions.begin() as s:
            for model in [KnowledgeEmbedding,RetrievalIndexDocument,RetrievalIndex,KnowledgeChunk,KnowledgeIngestion,KnowledgeDocument]:await s.execute(delete(model))
        self.workspace=self.root/uuid4().hex;self.workspace.mkdir();self.counter=0
        self.pipeline=IngestionPipeline(self.db,root=self.workspace)
        self.models=StubModels();self.adapter=EmbeddingAdapter(self.models)
        self.builder=DenseIndexBuilder(self.db,self.adapter,root=self.workspace)
        self.retriever=DenseRetriever(self.db,self.adapter,clock=lambda:date(2026,11,14))
        self.alice=IdentityContext(subject_id='alice',authenticated=True,mode='authenticated_session')
        self.bob=self.alice.model_copy(update={'subject_id':'bob'})

    async def asyncTearDown(self):await self.db.close()

    async def document(self,body='LOW',*,family=None,version=None,start='2020-01-01',end=None,scope='public',owner=None,status='published',source_type='business_document'):
        self.counter+=1;number=self.counter
        path=self.workspace/f'source-{number}.txt';path.write_text(f'{body} fixture {number}',encoding='utf-8')
        spec=DocumentSpec(source_key=family or f'source-{number}',title=f'Policy {number}',version=version or f'v{number}',domain='consumer_reference',
            access_scope=scope,owner_id=owner,effective_from=start,effective_until=end,source_type=source_type,publisher='Test fixture',license='test-only')
        result=await self.pipeline.ingest(path,spec)
        async with self.db.sessions.begin() as s:
            row=await s.get(KnowledgeDocument,result['document_id']);row.status=status
        return result['document_id']

    async def active_id(self):
        async with self.db.sessions() as s:return await s.scalar(select(RetrievalIndex.id).where(RetrievalIndex.active.is_(True)))

    async def visible(self,identity=None,day=None):
        async with self.db.sessions() as s:return set((await s.scalars(current_document_ids(identity,day or date(2026,11,14)))).all())

    def require_pg(self):
        if self.db.engine.dialect.name!='postgresql':self.skipTest('Exact vector ranking requires the real pgvector suite')

    async def test_staging_idempotence_and_atomic_activation(self):
        await self.document();first=await self.builder.build()
        self.assertIsNone(await self.active_id());self.assertFalse(first['active'])
        calls=len(self.models.inputs);second=await self.builder.build()
        self.assertTrue(second['unchanged']);self.assertEqual(len(self.models.inputs),calls)
        self.assertEqual(first['index_id'],second['index_id'])
        await self.builder.activate(first['index_id'],expected_active=None)
        self.assertEqual(await self.active_id(),first['index_id'])

    async def test_many_chunks_are_all_written_before_stage_commit(self):
        await self.document('bulk paragraph '*18000)
        index=await self.builder.build(activate=True)
        self.assertGreater(index['chunks'],200)
        async with self.db.sessions() as s:
            count=await s.scalar(select(func.count()).select_from(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index['index_id']))
            manifest=await s.scalar(select(RetrievalIndexDocument).where(RetrievalIndexDocument.index_id==index['index_id']))
            self.assertEqual(count,index['chunks']);self.assertEqual(count,len(manifest.snapshot['chunks']))

    async def test_failure_keeps_active_index_and_does_not_publish_partial_vectors(self):
        await self.document();old=await self.builder.build(activate=True)
        await self.document('another LOW')
        for invalid in ['raise','zero','dimension']:
            self.models.invalid=invalid
            with self.assertRaises((RuntimeError,ValueError)):await self.builder.build(activate=True)
            self.assertEqual(await self.active_id(),old['index_id'])
        async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(RetrievalIndex)),1)

    async def test_model_change_creates_distinct_vectors_and_old_index_is_retained(self):
        await self.document();old=await self.builder.build(activate=True)
        self.models.name='stub@'+'b'*64
        new=await self.builder.build()
        self.assertNotEqual(old['index_id'],new['index_id']);self.assertEqual(await self.active_id(),old['index_id'])
        await self.builder.activate(new['index_id'],expected_active=old['index_id'])
        self.assertEqual(await self.active_id(),new['index_id'])
        self.models.name='stub@'+'a'*64
        await self.builder.activate(old['index_id'],expected_active=new['index_id'])
        self.assertEqual(await self.active_id(),old['index_id'])

    async def test_wrong_model_and_active_compare_and_swap_prevent_switch(self):
        await self.document();old=await self.builder.build(activate=True)
        self.models.name='stub@'+'b'*64;new=await self.builder.build()
        with self.assertRaisesRegex(ApplicationError,'INDEX_ACTIVE_CHANGED'):await self.builder.activate(new['index_id'],expected_active='someone-else')
        self.models.name='stub@'+'a'*64
        with self.assertRaisesRegex(ApplicationError,'EMBEDDING_INDEX_IDENTITY_MISMATCH'):await self.builder.activate(new['index_id'],expected_active=old['index_id'])
        self.assertEqual(await self.active_id(),old['index_id'])

    async def test_missing_vector_and_modified_manifest_cannot_activate(self):
        await self.document();index=await self.builder.build()
        async with self.db.sessions.begin() as s:await s.execute(delete(KnowledgeEmbedding).where(KnowledgeEmbedding.index_id==index['index_id']))
        with self.assertRaisesRegex(ApplicationError,'INDEX_INCOMPLETE'):await self.builder.activate(index['index_id'],expected_active=None)
        self.assertIsNone(await self.active_id())

    async def test_edited_manifest_is_detected(self):
        await self.document();index=await self.builder.build()
        async with self.db.sessions.begin() as s:
            row=await s.scalar(select(RetrievalIndexDocument));row.snapshot={**row.snapshot,'tampered':True}
        with self.assertRaisesRegex(ApplicationError,'INDEX_MANIFEST_MISMATCH'):await self.builder.activate(index['index_id'],expected_active=None)

    async def test_metadata_changed_after_staging_requires_rebuild(self):
        identifier=await self.document();index=await self.builder.build()
        async with self.db.sessions.begin() as s:await s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id==identifier).values(title='changed'))
        with self.assertRaisesRegex(ApplicationError,'INDEX_CORPUS_CHANGED'):await self.builder.activate(index['index_id'],expected_active=None)

    async def test_mid_build_corpus_change_keeps_previous_active(self):
        identifier=await self.document();old=await self.builder.build(activate=True)
        await self.document('new data')
        real_documents=self.adapter.documents
        async def changing(texts,expected):
            vectors=await real_documents(texts,expected)
            async with self.db.sessions.begin() as s:await s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id==identifier).values(title='edited during build'))
            return vectors
        self.adapter.documents=changing
        with self.assertRaisesRegex(ApplicationError,'INDEX_CORPUS_CHANGED'):await self.builder.build(activate=True)
        self.assertEqual(await self.active_id(),old['index_id'])

    async def test_bad_chunk_hash_and_corrupt_original_cannot_be_indexed(self):
        identifier=await self.document()
        async with self.db.sessions.begin() as s:await s.execute(update(KnowledgeChunk).where(KnowledgeChunk.document_id==identifier).values(content='tampered'))
        with self.assertRaisesRegex(ApplicationError,'CHUNK_CONTENT_CHANGED'):await self.builder.build()

    async def test_drafts_and_synthetic_sources_are_not_admitted(self):
        await self.document(status='draft');await self.document(source_type='synthetic_demo')
        with self.assertRaisesRegex(ApplicationError,'EMPTY_INDEX_CORPUS'):await self.builder.build()
        self.assertEqual(self.models.inputs,[])

    async def test_expired_latest_revision_never_revives_old_version(self):
        await self.document('OLD_HIGH',family='policy',version='v1')
        await self.document('NEW_LOW',family='policy',version='v2',start='2025-01-01',end='2026-01-01')
        self.assertEqual(await self.visible(self.alice),set())

    async def test_future_revision_does_not_override_current_before_its_start(self):
        old=await self.document('OLD_LOW',family='policy',version='v1')
        future=await self.document('FUTURE_HIGH',family='policy',version='v2',start='2027-01-01')
        self.assertEqual(await self.visible(self.alice),{old})
        self.assertEqual(await self.visible(self.alice,date(2027,1,1)),{future})

    async def test_opaque_version_labels_and_withdrawal(self):
        await self.document('OLD_HIGH',family='policy',version='v10')
        latest=await self.document('LATEST_LOW',family='policy',version='v2')
        self.assertEqual(await self.visible(self.alice),{latest})
        async with self.db.sessions.begin() as s:await s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id==latest).values(status='withdrawn'))
        self.assertEqual(await self.visible(self.alice),set())

    async def test_private_scope_cannot_cross_owner_or_admin(self):
        public=await self.document();private=await self.document('PRIVATE_HIGH',scope='private',owner='bob')
        staff=await self.document('STAFF_HIGH',scope='support')
        self.assertEqual(await self.visible(self.alice),{public})
        self.assertEqual(await self.visible(self.bob),{public,private})
        admin=self.alice.model_copy(update={'role':'admin'})
        self.assertEqual(await self.visible(admin),{public,staff})

    async def test_changed_dimension_is_rejected_before_calls(self):
        self.models.dimensions=768
        with self.assertRaisesRegex(ApplicationError,'EMBEDDING_DIMENSION_MISMATCH'):await self.builder.build()
        self.assertEqual(self.models.inputs,[])

    async def test_exact_pgvector_acl_filter_is_before_top_k(self):
        self.require_pg()
        public=await self.document('PUBLIC_LOW');foreign=await self.document('PRIVATE_HIGH',scope='private',owner='bob')
        await self.builder.build(activate=True)
        own=await self.retriever.search('question',1,identity=self.alice)
        other=await self.retriever.search('question',1,identity=self.bob)
        self.assertEqual(own['documents'][0]['metadata']['document_id'],public)
        self.assertEqual(other['documents'][0]['metadata']['document_id'],foreign)
        self.assertEqual(own['search'],'exact_cosine');self.assertNotIn('embedding',own['documents'][0])

    async def test_exact_pgvector_version_filter_precedes_top_k(self):
        self.require_pg()
        await self.document('OLD_HIGH',family='policy',version='v1')
        latest=await self.document('NEW_LOW',family='policy',version='v2',start='2025-01-01')
        await self.builder.build(activate=True)
        rows=await self.retriever.search('question',1,identity=self.alice)
        self.assertEqual(rows['documents'][0]['metadata']['document_id'],latest)

    async def test_new_unindexed_revision_does_not_fall_back_to_old_vectors(self):
        self.require_pg()
        await self.document('OLD_HIGH',family='policy',version='v1');await self.builder.build(activate=True)
        await self.document('NEW_LOW',family='policy',version='v2',start='2025-01-01')
        self.assertEqual((await self.retriever.search('question',5,identity=self.alice))['documents'],[])

    async def test_query_model_mismatch_and_mid_query_replacement_are_rejected(self):
        self.require_pg()
        await self.document();await self.builder.build(activate=True)
        self.models.name='stub@'+'b'*64
        with self.assertRaisesRegex(ApplicationError,'EMBEDDING_INDEX_IDENTITY_MISMATCH'):await self.retriever.search('question',identity=self.alice)
        self.models.name='stub@'+'a'*64;self.models.change_on_query=True
        with self.assertRaisesRegex(ApplicationError,'EMBEDDING_MODEL_CHANGED'):await self.retriever.search('question',identity=self.alice)

    async def test_revocation_between_search_and_context_is_rechecked(self):
        self.require_pg()
        identifier=await self.document('PRIVATE_HIGH',scope='private',owner='alice');await self.builder.build(activate=True)
        result=await self.retriever.search('question',identity=self.alice)
        async with self.db.sessions.begin() as s:await s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id==identifier).values(owner_id='bob'))
        with self.assertRaisesRegex(ApplicationError,'RETRIEVAL_AUTHORIZATION_CHANGED'):await self.retriever.revalidate(result['documents'],self.alice)
        with self.assertRaisesRegex(ApplicationError,'INDEX_DOCUMENT_STALE'):await self.retriever.search('question',identity=self.bob)

    async def test_old_unverified_index_is_not_silently_used(self):
        self.require_pg()
        async with self.db.sessions.begin() as s:s.add(RetrievalIndex(id='legacy',active=True,model_identity={},configuration={}))
        with self.assertRaisesRegex(ApplicationError,'INDEX_UPGRADE_REQUIRED'):await self.retriever.search('question',identity=self.alice)

    async def test_original_vectors_are_not_reused_as_model_output(self):
        identifier=await self.document('HIGH')
        async with self.db.sessions.begin() as s:
            s.add(RetrievalIndex(id='legacy-demo',active=True,model_identity={},configuration={}))
            await s.flush();chunk=await s.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id==identifier))
            s.add(KnowledgeEmbedding(index_id='legacy-demo',chunk_id=chunk.id,embedding=[0.,1.]+[0.]*1022))
        index=await self.builder.build(activate=True)
        async with self.db.sessions() as s:
            vector=await s.scalar(select(KnowledgeEmbedding.embedding).where(KnowledgeEmbedding.index_id==index['index_id']))
            old=await s.scalar(select(KnowledgeEmbedding.embedding).where(KnowledgeEmbedding.index_id=='legacy-demo'))
            self.assertAlmostEqual(float(vector[0]),1.);self.assertAlmostEqual(float(old[0]),0.)

    async def test_search_body_cannot_supply_actor_date_or_index(self):
        from pydantic import ValidationError
        from smart_cs.schemas.retrieval import RetrievalRequest
        for payload in [{'query':'q','user_id':'bob'},{'query':'q','as_of':'2027-01-01'},{'query':'q','index_id':'legacy'},
            {'query':'q','top_k':51},{'query':' '}]:
            with self.assertRaises(ValidationError):RetrievalRequest.model_validate(payload)

    async def anchored_citations(self,*,scope='public',owner=None):
        from smart_cs.retrieval.grounding import GroundedAnswerService
        from smart_cs.schemas.grounding import AnswerDraft
        identifier=await self.document('Policy refunds require a verified receipt and an eligible order.',scope=scope,owner=owner)
        await self.builder.build(activate=True)
        async with self.db.sessions() as s:
            document=await s.get(KnowledgeDocument,identifier)
            chunk=await s.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id==identifier))
            index=await s.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
            evidence=self.retriever.serialize(chunk,document,index,0)
        draft=AnswerDraft(status='answered',claims=[{'text':'A receipt is needed.',
            'references':[{'chunk_id':evidence['id'],'quote':evidence['content']}]}])
        citations,_=GroundedAnswerService.anchors(draft,[evidence])
        return identifier,citations

    async def test_citation_resolves_actual_version_block_and_quote_offsets(self):
        from copy import deepcopy
        _,citations=await self.anchored_citations()
        await self.retriever.revalidate_citations(citations,self.alice)
        for field,value in [('version','invented'),('document_id','other-document'),('url','https://evil.invalid'),('chunk_sha256','f'*64),('index_id','old-index')]:
            changed=deepcopy(citations);changed[0][field]=value
            with self.assertRaisesRegex(ApplicationError,'CITATION_BINDING_CHANGED'):await self.retriever.revalidate_citations(changed,self.alice)
        for change in [{'text':'fabricated quote'},{'start':-1},{'end':999999},{'start':False}]:
            changed=deepcopy(citations);changed[0]['quotes'][0].update(change)
            with self.assertRaisesRegex(ApplicationError,'CITATION_QUOTE_INVALID'):await self.retriever.revalidate_citations(changed,self.alice)

    async def test_citation_cannot_cross_owner_even_for_admin(self):
        _,citations=await self.anchored_citations(scope='private',owner='alice')
        await self.retriever.revalidate_citations(citations,self.alice)
        for identity in [self.bob,self.bob.model_copy(update={'role':'admin'})]:
            with self.assertRaisesRegex(ApplicationError,'RETRIEVAL_AUTHORIZATION_CHANGED'):await self.retriever.revalidate_citations(citations,identity)

    async def test_citation_withdrawn_or_expired_after_generation_is_not_released(self):
        identifier,citations=await self.anchored_citations()
        async with self.db.sessions.begin() as s:await s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id==identifier).values(status='withdrawn'))
        with self.assertRaisesRegex(ApplicationError,'RETRIEVAL_AUTHORIZATION_CHANGED'):await self.retriever.revalidate_citations(citations,self.alice)

    async def test_block_locator_applies_owner_acl_and_exposes_historical_validity(self):
        import httpx
        from unittest.mock import patch
        from smart_cs.api import main
        from smart_cs.api.dependencies import current_identity
        identifier,citations=await self.anchored_citations(scope='private',owner='alice')
        block=citations[0]['block_url']
        original=dict(main.app.dependency_overrides)
        try:
            with patch.object(main,'database',self.db),patch.dict(os.environ,{'APP_MODE':'local_demo'}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://testserver') as client:
                    for identity,expected in [(self.alice,200),(self.bob,404),(self.bob.model_copy(update={'role':'admin'}),404)]:
                        main.app.dependency_overrides[current_identity]=lambda:identity
                        response=await client.get(block)
                        self.assertEqual(response.status_code,expected)
                        if expected==200:
                            self.assertTrue(response.json()['currently_effective'])
                            self.assertEqual(response.json()['chunk_sha256'],citations[0]['chunk_sha256'])
                            self.assertEqual(response.headers['cache-control'],'no-store')
                    main.app.dependency_overrides[current_identity]=lambda:self.alice
                    async with self.db.sessions.begin() as s:await s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id==identifier).values(effective_until='2025-01-01'))
                    response=await client.get(block)
                    self.assertEqual(response.status_code,200);self.assertFalse(response.json()['currently_effective'])
                    self.assertEqual((await client.get(block.replace(identifier,'wrong-document'))).status_code,404)
        finally:
            main.app.dependency_overrides.clear();main.app.dependency_overrides.update(original)
