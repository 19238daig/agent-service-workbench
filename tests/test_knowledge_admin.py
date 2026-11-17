"""Operator publication and activation contracts in disposable SQLite/PG databases."""
import asyncio
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch
from sqlalchemy import select, delete, func
from smart_cs.db.database import Database
from smart_cs.db.models import User, AuthCredential, AuthSession, KnowledgeDocument, KnowledgeChunk, KnowledgeIngestion, KnowledgeEmbedding, RetrievalIndex, RetrievalIndexDocument, KnowledgeAdminEvent, utcnow
from smart_cs.ingestion.pipeline import IngestionPipeline
from smart_cs.schemas.ingestion import DocumentSpec
from smart_cs.schemas.knowledge_admin import DocumentCommand, ActivateIndex
from smart_cs.schemas.identity import IdentityContext
from smart_cs.schemas.errors import ApplicationError
from smart_cs.services.knowledge_admin import KnowledgeAdminService
from smart_cs.retrieval.embedding import EmbeddingAdapter
from smart_cs.retrieval.indexes import DenseIndexBuilder
from test_dense_retrieval import StubModels


class KnowledgeAdminTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cls.url='sqlite+aiosqlite:///'+(cls.root/'test.db').as_posix();cls.schema=None
        if os.getenv('AUTH_VERIFY_SCHEMA'):cls.url=os.environ['DATABASE_URL'];cls.schema=os.environ['AUTH_VERIFY_SCHEMA']
        async def setup():
            db=Database(cls.url,schema=cls.schema)
            try:
                await db.initialize(create_schema=True)
                async with db.sessions.begin() as s:
                    s.add_all([User(id='operator',display_name='Operator',role='admin'),User(id='customer',display_name='Customer')]);await s.flush()
                    s.add(AuthCredential(user_id='operator',login_name='operator',password_hash='test-only-not-used',enabled=True))
            finally:await db.close()
        asyncio.run(setup())

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    async def asyncSetUp(self):
        self.db=Database(self.url,schema=self.schema);self.workspace=self.root/uuid4().hex;self.workspace.mkdir()
        async with self.db.sessions.begin() as s:
            for model in [KnowledgeAdminEvent,KnowledgeEmbedding,RetrievalIndexDocument,RetrievalIndex,KnowledgeChunk,KnowledgeIngestion,KnowledgeDocument,AuthSession]:await s.execute(delete(model))
            credential=await s.get(AuthCredential,'operator');credential.enabled=True
            user=await s.get(User,'operator');user.role='admin'
        self.admin=IdentityContext(subject_id='operator',role='admin',authenticated=True,mode='authenticated_session')
        self.adapter=EmbeddingAdapter(StubModels());self.service=KnowledgeAdminService(self.db,self.workspace,self.adapter)
        path=self.workspace/'policy.html';path.write_text('<main><h1>Fixture</h1><p>LOW policy '+uuid4().hex+'</p><script>alert(1)</script></main>',encoding='utf-8')
        spec=DocumentSpec(source_key=uuid4().hex,title='Fixture',version='v1',domain='consumer_reference',access_scope='private',owner_id='operator',effective_from='2020-01-01',source_type='business_document',publisher='test',license='test-only')
        result=await IngestionPipeline(self.db,root=self.workspace).ingest(path,spec);self.identifier=result['document_id']
        async with self.db.sessions() as s:
            d=await s.get(KnowledgeDocument,self.identifier);self.command=DocumentCommand(content_hash=d.content_hash,version=d.version)

    async def asyncTearDown(self):await self.db.close()

    async def audit_count(self):
        async with self.db.sessions() as s:return await s.scalar(select(func.count()).select_from(KnowledgeAdminEvent))

    async def test_list_paginates_and_filters_managed_documents(self):
        listing=await self.service.list(self.admin,1,0,'draft');self.assertEqual(listing['total'],1);self.assertEqual(listing['documents'][0]['id'],self.identifier);self.assertGreater(listing['documents'][0]['chunks'],0)
        self.assertEqual((await self.service.list(self.admin,1,1,'draft'))['documents'],[])
        self.assertEqual((await self.service.list(self.admin,status='published'))['total'],0)

    async def test_draft_preview_and_download_verify_snapshot_without_executing_html(self):
        view=await self.service.detail(self.admin,self.identifier);self.assertEqual(view['status'],'draft');self.assertNotIn('alert(',str(view['preview']))
        path,_,mime=await self.service.file(self.admin,self.identifier);self.assertEqual(mime,'text/html');self.assertEqual(path.suffix,'.html')
        from smart_cs.api.knowledge_admin import file
        class State:database=self.db
        class App:state=State()
        class Request:app=App()
        with patch('smart_cs.api.knowledge_admin.service',return_value=self.service):response=await file(self.identifier,Request(),self.admin)
        self.assertTrue(response.headers['content-type'].startswith('text/plain'));self.assertEqual(response.headers['content-security-policy'],'sandbox')

    async def test_customer_forged_admin_disabled_and_revoked_sessions_are_rejected(self):
        for actor in [self.admin.model_copy(update={'role':'customer'}),self.admin.model_copy(update={'subject_id':'customer'})]:
            with self.assertRaises(ApplicationError):await self.service.list(actor)
        async with self.db.sessions.begin() as s:(await s.get(AuthCredential,'operator')).enabled=False
        with self.assertRaises(ApplicationError):await self.service.change(self.admin,self.identifier,self.command,True)
        self.assertEqual(await self.audit_count(),0)

    async def test_expired_or_revoked_admin_session_cannot_publish(self):
        key=uuid4().hex
        async with self.db.sessions.begin() as s:s.add(AuthSession(id=key,user_id='operator',access_hash='a'*64,refresh_hash='b'*64,csrf_hash='c'*64,access_expires_at=utcnow()-timedelta(seconds=1),refresh_expires_at=utcnow()+timedelta(hours=1)))
        actor=self.admin.model_copy(update={'auth_session_id':key})
        with self.assertRaises(ApplicationError):await self.service.change(actor,self.identifier,self.command,True)
        async with self.db.sessions.begin() as s:
            row=await s.get(AuthSession,key);row.access_expires_at=utcnow()+timedelta(hours=1);row.revoked_at=utcnow()
        with self.assertRaises(ApplicationError):await self.service.change(actor,self.identifier,self.command,True)
        self.assertEqual(await self.audit_count(),0)

    async def test_publish_withdraw_are_explicit_version_bound_and_audited_once(self):
        with self.assertRaises(ApplicationError):await self.service.change(self.admin,self.identifier,self.command.model_copy(update={'version':'wrong'}),True)
        self.assertEqual(await self.audit_count(),0)
        first=await self.service.change(self.admin,self.identifier,self.command,True);self.assertEqual(first['document']['status'],'published');self.assertFalse(first['active_index_changed'])
        self.assertTrue((await self.service.change(self.admin,self.identifier,self.command,True))['unchanged']);self.assertEqual(await self.audit_count(),1)
        await self.service.change(self.admin,self.identifier,self.command,False);await self.service.change(self.admin,self.identifier,self.command,False)
        self.assertEqual(await self.audit_count(),2)
        with self.assertRaises(ApplicationError):await self.service.change(self.admin,self.identifier,self.command,True)
        self.assertEqual(await self.audit_count(),2)

    async def test_tampered_file_and_receipt_binding_block_publish_without_audit(self):
        async with self.db.sessions.begin() as s:(await s.get(KnowledgeDocument,self.identifier)).owner_id='customer'
        with self.assertRaises(ApplicationError):await self.service.change(self.admin,self.identifier,self.command,True)
        async with self.db.sessions.begin() as s:(await s.get(KnowledgeDocument,self.identifier)).owner_id='operator'
        path,_,_=await self.service.file(self.admin,self.identifier);path.write_bytes(b'corrupt')
        with self.assertRaises(ApplicationError):await self.service.change(self.admin,self.identifier,self.command,True)
        self.assertEqual(await self.audit_count(),0)
        self.assertEqual((await self.service.detail(self.admin,self.identifier))['status'],'draft')

    async def test_audit_failure_rolls_back_document_publication(self):
        with patch('smart_cs.services.knowledge_admin.audit',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):await self.service.change(self.admin,self.identifier,self.command,True)
        self.assertEqual((await self.service.detail(self.admin,self.identifier))['status'],'draft');self.assertEqual(await self.audit_count(),0)

    async def test_activation_validates_cas_and_audit_is_atomic_with_switch(self):
        await self.service.change(self.admin,self.identifier,self.command,True)
        index=await DenseIndexBuilder(self.db,self.adapter,root=self.workspace).build()
        self.assertIsNone((await self.service.indexes(self.admin))['active_index_id'])
        with self.assertRaises(ApplicationError):await self.service.activate(self.admin,index['index_id'],'wrong')
        with patch('smart_cs.services.knowledge_admin.audit',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):await self.service.activate(self.admin,index['index_id'],None)
        self.assertIsNone((await self.service.indexes(self.admin))['active_index_id']);self.assertEqual(await self.audit_count(),1)
        await self.service.activate(self.admin,index['index_id'],None);self.assertEqual((await self.service.indexes(self.admin))['active_index_id'],index['index_id']);self.assertEqual(await self.audit_count(),2)
        await self.service.activate(self.admin,index['index_id'],index['index_id']);self.assertEqual(await self.audit_count(),2)

    async def test_path_escape_and_unmanaged_document_are_rejected(self):
        async with self.db.sessions.begin() as s:(await s.get(KnowledgeDocument,self.identifier)).file_path='../outside.html'
        with self.assertRaises(ApplicationError):await self.service.file(self.admin,self.identifier)
        async with self.db.sessions.begin() as s:await s.execute(delete(KnowledgeIngestion).where(KnowledgeIngestion.document_id==self.identifier))
        with self.assertRaises(ApplicationError):await self.service.change(self.admin,self.identifier,self.command,True)

    def test_commands_forbid_actor_and_require_explicit_active_version(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):DocumentCommand.model_validate({**self.command.model_dump(),'actor':'admin'})
        with self.assertRaises(ValidationError):ActivateIndex.model_validate({})
