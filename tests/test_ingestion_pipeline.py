"""Operator ingestion contract on SQLite and the same restricted PostgreSQL suite."""
import asyncio
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import select,func
from pypdf import PdfWriter
from smart_cs.db.database import Database
from smart_cs.db.models import User,KnowledgeDocument,KnowledgeChunk,KnowledgeIngestion,RetrievalIndex
from smart_cs.ingestion.pipeline import IngestionPipeline,IngestionError
from smart_cs.ingestion.parsers import parse
from smart_cs.ingestion.cli import main as cli_main
from smart_cs.schemas.ingestion import DocumentSpec,IngestionOptions
from smart_cs.services.access import document_visibility
from smart_cs.schemas.identity import IdentityContext


class IngestionPipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cls.url='sqlite+aiosqlite:///'+(cls.root/'knowledge.db').as_posix();cls.schema=None
        if os.getenv('AUTH_VERIFY_SCHEMA'):cls.url=os.environ['DATABASE_URL'];cls.schema=os.environ['AUTH_VERIFY_SCHEMA']
        async def seed():
            db=Database(cls.url,schema=cls.schema)
            try:
                await db.initialize(create_schema=True)
                async with db.sessions.begin() as s:
                    s.add_all([User(id='alice',display_name='Alice'),User(id='bob',display_name='Bob')])
            finally:await db.close()
        asyncio.run(seed())

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    async def asyncSetUp(self):
        self.db=Database(self.url,schema=self.schema)
        self.key=uuid4().hex;self.workspace=self.root/'runs'/self.key;self.pipeline=IngestionPipeline(self.db,root=self.workspace)
        self.inputs=self.workspace/'inputs';self.inputs.mkdir(parents=True)
        self.spec=DocumentSpec(source_key='test-'+self.key,title='Test policy',version='v-'+self.key[:12],domain='test_reference',
            access_scope='public',effective_from='2020-01-01',source_type='test_fixture',publisher='Test suite',license='test-only')

    async def asyncTearDown(self):await self.db.close()

    def file(self,name='source.md',body=None):
        if body is None:body='# Heading\nPolicy text.\n\n## Details\nMore information. Case '+self.key
        path=self.inputs/name;path.write_text(body,encoding='utf-8');return path

    async def chunks(self,identifier):
        async with self.db.sessions() as s:
            return list((await s.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id==identifier).order_by(KnowledgeChunk.ordinal))).all())

    async def test_markdown_adjacent_headings_fences_and_txt_hash_lines_are_not_lost(self):
        path=self.file(body='# Title\nBody immediately follows.\n```python\n# this is code\nprint(1)\n```\n## Later\nLast body.')
        blocks=parse(path)
        self.assertIn('Body immediately follows.',' '.join(b.text for b in blocks))
        code=next(b for b in blocks if b.kind=='code');self.assertIn('# this is code',code.text)
        self.assertEqual((code.line_start,code.line_end),(3,6))
        txt=self.file('source.txt','# ordinary text\nnot a Markdown heading')
        self.assertEqual(parse(txt)[0].text,'# ordinary text\nnot a Markdown heading')
        self.assertEqual(parse(txt)[0].line_start,1)

    async def test_repeat_is_idempotent_and_skips_reparse(self):
        path=self.file();first=await self.pipeline.ingest(path,self.spec,publish=True)
        with patch('smart_cs.ingestion.pipeline.prepare_document',side_effect=AssertionError('must not reparse')):
            second=await self.pipeline.ingest(path,self.spec,publish=True)
        self.assertFalse(first['unchanged']);self.assertTrue(second['unchanged'])
        self.assertEqual(first['document_id'],second['document_id'])
        rows=await self.chunks(first['document_id']);self.assertEqual(len(rows),first['chunks'])
        self.assertTrue(all(c.embedding_status=='pending' for c in rows))
        self.assertTrue(all(c.locator['artifact_sha256']==first['artifact_sha256'] for c in rows))

    async def test_copy_alias_reuses_same_artifact_and_acl(self):
        path=self.file();first=await self.pipeline.ingest(path,self.spec)
        copy=self.inputs/'copy.md';copy.write_bytes(path.read_bytes())
        alias=self.spec.model_copy(update={'source_key':'alias-'+self.key})
        second=await self.pipeline.ingest(copy,alias)
        self.assertEqual(first['document_id'],second['document_id']);self.assertTrue(second['duplicate_content'])

    async def test_same_version_content_or_configuration_conflict_keeps_old_chunks(self):
        path=self.file();first=await self.pipeline.ingest(path,self.spec)
        snapshot=self.workspace/first['file_path'];original=snapshot.read_bytes()
        path.write_text('changed content',encoding='utf-8')
        with self.assertRaisesRegex(IngestionError,'IMMUTABLE_VERSION_CONFLICT'):await self.pipeline.ingest(path,self.spec)
        self.assertEqual(snapshot.read_bytes(),original)
        path.write_bytes(original)
        with self.assertRaisesRegex(IngestionError,'IMMUTABLE_VERSION_CONFLICT'):
            await self.pipeline.ingest(path,self.spec,IngestionOptions(max_chars=700))
        self.assertEqual(len(await self.chunks(first['document_id'])),first['chunks'])

    async def test_new_version_is_a_new_document_without_rewriting_previous_version(self):
        path=self.file();first=await self.pipeline.ingest(path,self.spec)
        path.write_text('Changed text with a declared new version.',encoding='utf-8')
        next_spec=self.spec.model_copy(update={'version':'next-'+self.key[:12]})
        second=await self.pipeline.ingest(path,next_spec)
        self.assertNotEqual(first['document_id'],second['document_id'])
        self.assertTrue((await self.chunks(first['document_id']))[0].content)

    async def test_concurrent_duplicates_have_one_document_and_one_chunk_set(self):
        path=self.file();results=await asyncio.gather(self.pipeline.ingest(path,self.spec),self.pipeline.ingest(path,self.spec))
        self.assertEqual(len({r['document_id'] for r in results}),1)
        self.assertEqual(sum(not r['unchanged'] for r in results),1)
        self.assertEqual(len(await self.chunks(results[0]['document_id'])),results[0]['chunks'])

    async def test_private_scope_has_separate_dedupe_and_visibility(self):
        path=self.file();private=self.spec.model_copy(update={'access_scope':'private','owner_id':'alice'})
        own=await self.pipeline.ingest(path,private,publish=True)
        public=await self.pipeline.ingest(path,self.spec,publish=True)
        self.assertNotEqual(own['document_id'],public['document_id'])
        async with self.db.sessions() as s:
            for user,expected in [('alice',True),('bob',False)]:
                principal=IdentityContext(subject_id=user,authenticated=True,mode='authenticated_session')
                result=await s.scalar(select(KnowledgeDocument.id).where(KnowledgeDocument.id==own['document_id'],document_visibility(principal)))
                self.assertEqual(bool(result),expected)

    async def test_unknown_private_owner_does_not_leave_document_or_chunks(self):
        path=self.file();private=self.spec.model_copy(update={'access_scope':'private','owner_id':'unknown'})
        with self.assertRaisesRegex(IngestionError,'UNKNOWN_OWNER'):await self.pipeline.ingest(path,private)
        async with self.db.sessions() as s:
            self.assertIsNone(await s.scalar(select(KnowledgeIngestion.id).where(KnowledgeIngestion.source_key==private.source_key)))

    async def test_legacy_published_artifact_rejects_duplicate_publication_atomically(self):
        path=self.file();sha=hashlib.sha256(path.read_bytes()).hexdigest();legacy='legacy-'+self.key
        async with self.db.sessions.begin() as s:
            s.add(KnowledgeDocument(id=legacy,source='legacy.md',title='legacy',version=self.spec.version,domain='test_reference',
                content='legacy',content_hash=sha,effective_from='2020-01-01',status='published'))
        with self.assertRaisesRegex(IngestionError,'ARTIFACT_ALREADY_PUBLISHED'):await self.pipeline.ingest(path,self.spec,publish=True)
        async with self.db.sessions() as s:
            self.assertIsNone(await s.scalar(select(KnowledgeIngestion).where(KnowledgeIngestion.source_key==self.spec.source_key)))
            self.assertEqual((await s.get(KnowledgeDocument,legacy)).content,'legacy')

    async def test_draft_publish_is_explicit_and_does_not_activate_index(self):
        first=await self.pipeline.ingest(self.file(),self.spec);self.assertEqual(first['status'],'draft')
        self.assertEqual((await self.pipeline.publish(first['document_id']))['status'],'published')
        self.assertEqual((await self.pipeline.publish(first['document_id']))['status'],'published')
        async with self.db.sessions() as s:self.assertEqual(await s.scalar(select(func.count()).select_from(RetrievalIndex)),0)

    async def test_corrupt_snapshot_is_rejected_without_overwriting_evidence(self):
        path=self.file();first=await self.pipeline.ingest(path,self.spec)
        snapshot=self.workspace/first['file_path'];snapshot.write_bytes(b'tampered')
        with self.assertRaisesRegex(IngestionError,'SNAPSHOT_INTEGRITY_FAILED'):await self.pipeline.ingest(path,self.spec)
        with self.assertRaisesRegex(IngestionError,'SNAPSHOT_INTEGRITY_FAILED'):await self.pipeline.publish(first['document_id'])
        self.assertEqual(snapshot.read_bytes(),b'tampered')

    async def test_bounded_chunks_reconstruct_long_text_and_point_to_line_ranges(self):
        body='第十一阶段测试，来源定位必须完整。'*180
        first=await self.pipeline.ingest(self.file('source.txt',body),self.spec,IngestionOptions(max_chars=240,overlap_chars=20))
        rows=await self.chunks(first['document_id']);covered=set()
        for row in rows:
            loc=row.locator;self.assertEqual(row.content,body[loc['char_start']:loc['char_end']])
            self.assertLessEqual(len(row.content),240);covered.update(range(loc['char_start'],loc['char_end']))
            self.assertEqual(loc['line_start'],1)
        self.assertEqual(len(covered),len(body))

    async def test_blank_pdf_and_empty_control_only_text_do_not_create_documents(self):
        path=self.inputs/'blank.pdf';writer=PdfWriter();writer.add_blank_page(width=300,height=300);writer.write(path)
        with self.assertRaisesRegex(ValueError,'OCR'):await self.pipeline.ingest(path,self.spec)
        with self.assertRaisesRegex(ValueError,'EMPTY_DOCUMENT'):await self.pipeline.ingest(self.file('empty.txt','\x00\x01\u200b'),self.spec)

    async def test_checksum_and_owner_metadata_fail_before_import(self):
        with self.assertRaisesRegex(IngestionError,'SOURCE_CHECKSUM_MISMATCH'):
            await self.pipeline.ingest(self.file(),self.spec.model_copy(update={'expected_sha256':'0'*64}))
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):DocumentSpec.model_validate({**self.spec.model_dump(),'access_scope':'private'})
        with self.assertRaises(ValidationError):DocumentSpec.model_validate({**self.spec.model_dump(),'source_url':'https://host/file?api_key=private-value'})

    async def test_html_removes_scripts_and_preserves_anchor_table_locators(self):
        path=self.file('source.html','<html><meta charset="utf-8"><nav>noise</nav><main><h1 id="rules">规则</h1><p>真实正文</p><table><tr><th>状态</th></tr><tr><td>处理中</td></tr></table><script>secret_script</script></main></html>')
        first=await self.pipeline.ingest(path,self.spec)
        rows=await self.chunks(first['document_id']);self.assertTrue(any(c.kind=='table' for c in rows))
        self.assertTrue(any(c.anchor=='rules' for c in rows));self.assertNotIn('secret_script',''.join(c.content for c in rows))

    async def test_bge_chunking_is_optional_and_keeps_explicit_model_identity(self):
        class Models:
            def identity(self):return {'embedding':{'digest':'test-only-model'}}
            def tokenize(self,texts,**kwargs):return [[{'text':t,'tokens':20}] for t in texts]
            def embed(self,texts):return [[1.,0.] for _ in texts]
        pipeline=IngestionPipeline(self.db,root=self.workspace,models=Models())
        result=await pipeline.ingest(self.file(),self.spec,IngestionOptions(chunker='bge-semantic'))
        rows=await self.chunks(result['document_id']);self.assertTrue(all(c.locator['precision']=='page_heading' for c in rows))
        async with self.db.sessions() as s:
            doc=await s.get(KnowledgeDocument,result['document_id'])
            self.assertEqual(doc.metadata_json['ingestion']['configuration']['embedding_model_for_chunking']['digest'],'test-only-model')


class IngestionCommandTests(unittest.TestCase):
    def test_inspection_works_without_database_and_outputs_locators_not_full_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.md';path.write_text('# Title\nExample body.',encoding='utf-8')
            spec=Path(folder)/'metadata.json';spec.write_text(json.dumps({'source_key':'example','title':'Example','version':'v1',
                'domain':'test','access_scope':'public','effective_from':'2020-01-01','source_type':'test','publisher':'test','license':'test-only'}),encoding='utf-8')
            output=io.StringIO()
            with redirect_stdout(output),patch('smart_cs.ingestion.cli.configured_database',side_effect=AssertionError('must not connect')):
                result=cli_main(['inspect',str(path),'--metadata',str(spec)])
            self.assertEqual(result,0);value=json.loads(output.getvalue());self.assertTrue(value['passed'])
            self.assertIn('sample_locators',value);self.assertNotIn('content',value)
