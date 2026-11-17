from legacy_identity import install_offline_identity
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from sqlalchemy import select

import smart_cs.api.main as main
from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.datasets import build_assets, json_bytes, sha, validate_assets
from smart_cs.db.database import Database
from smart_cs.db.models import Order, TicketEvent, User
from smart_cs.db.seed import seed_database
from smart_cs.db.tickets import DatabaseTicketStore
from smart_cs.memory.lexical_knowledge import LexicalKnowledgeMemory
from smart_cs.memory.short_term import ShortTermMemory


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = build_assets(self.root)

    def test_split_counts_and_provenance_are_explicit(self):
        self.assertEqual(self.report['total_samples'], 670)
        self.assertEqual(self.report['counts']['intent/train'], 350)
        self.assertEqual(self.report['counts']['intent/validation'], 50)
        self.assertEqual(self.report['counts']['intent/test'], 100)
        self.assertFalse(self.report['human_reviewed'])
        self.assertEqual(self.report['groups'], 170)

    def test_scenario_leak_is_detected_even_with_updated_checksums(self):
        train = json.loads((self.root / 'intent/train.jsonl').read_text(encoding='utf-8').splitlines()[0])
        target = self.root / 'intent/test.jsonl'
        rows = [json.loads(line) for line in target.read_text(encoding='utf-8').splitlines()]
        rows[0]['group_id'] = train['group_id']
        data = ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in rows).encode()
        target.write_bytes(data)
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        manifest['files']['intent/test.jsonl']['sha256'] = sha(data)
        (self.root / 'manifest.json').write_bytes(json_bytes(manifest))
        with self.assertRaisesRegex(ValueError, 'group leaked'):
            validate_assets(self.root)

    def test_release_rebuild_is_identical_and_modified_asset_is_not_overwritten(self):
        self.assertEqual(build_assets(self.root), self.report)
        path = self.root / 'knowledge/refund_policy.md'
        path.write_text('edited locally', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'new dataset version'):
            build_assets(self.root)
        self.assertEqual(path.read_text(encoding='utf-8'), 'edited locally')

    def test_lexical_baseline_finds_evidence_and_does_not_invent_vectors(self):
        documents = json.loads((self.root / 'knowledge/documents.json').read_text(encoding='utf-8'))
        memory = LexicalKnowledgeMemory(documents)
        matches = memory.search('签收后7日内未使用且包装完好普通商品的无理由退货')
        self.assertEqual(matches[0]['source'], 'refund_policy.md')
        self.assertEqual(memory.search('火星轨道燃料补给站'), [])
        self.assertEqual(memory.retrieval_mode, 'lexical_bootstrap_bm25')
        self.assertNotIn('reference_answer', '\n'.join(d['content'] for d in documents))


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = 'sqlite+aiosqlite:///' + str(self.root / 'database.sqlite3').replace('\\', '/')
        self.db = Database(self.url)
        await self.db.initialize(create_schema=True)
        self.store = DatabaseTicketStore(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_ticket_survives_new_engine_and_owner_filter(self):
        ticket = await self.store.create('refund', 'medium', '申请退款', '仅登记申请', 'owner-a')
        await self.db.close()
        self.db = Database(self.url)
        await self.db.initialize()
        store = DatabaseTicketStore(self.db)
        self.assertIsNotNone(await store.query(ticket['ticket_id'], 'owner-a'))
        self.assertIsNone(await store.query(ticket['ticket_id'], 'owner-b'))
        self.assertEqual((await self.db.summary())['counts']['ticket_events'], 1)

    async def test_invalid_order_rolls_back_user_ticket_and_event(self):
        with self.assertRaises(ValueError):
            await self.store.create('refund', 'medium', '错误订单', '', 'new-owner', 'NONEXISTENT')
        counts = (await self.db.summary())['counts']
        self.assertEqual((counts['users'], counts['tickets'], counts['ticket_events']), (0, 0, 0))

    async def test_transition_is_atomic_and_invalid_transition_is_rejected(self):
        ticket = await self.store.create('general', 'low', '服务记录', '', 'owner')
        with self.assertRaises(ValueError):
            await self.store.update_status(ticket['ticket_id'], 'resolved', 'owner')
        self.assertEqual((await self.db.summary())['counts']['ticket_events'], 1)
        updated = await self.store.update_status(ticket['ticket_id'], 'processing', 'owner')
        self.assertEqual((updated['status'], updated['version']), ('processing', 2))
        self.assertIsNone(await self.store.update_status(ticket['ticket_id'], 'resolved', 'other'))
        self.assertEqual((await self.db.summary())['counts']['ticket_events'], 2)

    async def test_seed_is_idempotent_and_preserves_runtime_state(self):
        assets = self.root / 'assets'
        build_assets(assets)
        first = await seed_database(self.db, assets)
        await self.store.update_status('TK-20261113-000001', 'resolved', 'demo-user-001')
        second = await seed_database(self.db, assets)
        self.assertEqual(first['database']['counts']['tickets'], second['database']['counts']['tickets'])
        self.assertEqual((await self.store.query('TK-20261113-000001', 'demo-user-001'))['status'], 'resolved')
        self.assertEqual(second['database']['counts']['dataset_samples'], 670)
        self.assertEqual(second['database']['counts']['knowledge_documents'], 12)


class DatabaseApiTests(unittest.TestCase):
    def setUp(self):
        install_offline_identity(self, main.app)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.url = 'sqlite+aiosqlite:///' + str(Path(self.temp.name) / 'api.sqlite3').replace('\\', '/')
        async def initialize():
            database = Database(self.url)
            try:
                await database.initialize(create_schema=True)
            finally:
                await database.close()
        asyncio.run(initialize())

    def client(self, responses):
        fake = FakeListChatModel(responses=responses)
        patches = [patch.object(main, 'configured_database', side_effect=lambda: Database(self.url)),
                   patch.object(main, 'graph', None), patch.object(main, 'init_tracer'),
                   patch.object(main, 'ENV_PATH', Path(self.temp.name) / '.env'),
                   patch.object(main, 'short_term_memory', ShortTermMemory('memory://')),
                   patch.object(main, 'long_term_memory', LexicalKnowledgeMemory()),
                   patch.object(main, 'create_supervisor_graph', side_effect=lambda **kw: create_supervisor_graph(llm=fake, **kw)),
                   patch.dict(os.environ, {'OPENAI_API_KEY': 'offline-test', 'APP_MODE': 'local_demo'})]
        for value in patches:
            value.start()
            self.addCleanup(value.stop)
        return TestClient(main.app)

    def test_real_agent_database_injection_and_restarted_api_read(self):
        # Explicit authenticated principal fixture for the typed live tool port;
        # real password/session verification is covered by test_auth.
        from legacy_identity import offline_identity
        from smart_cs.api.dependencies import current_identity
        from fastapi import Request
        async def authenticated_fixture(request:Request):
            identity=await offline_identity(request)
            return identity.model_copy(update={'authenticated':True,'mode':'authenticated_session'})
        main.app.dependency_overrides[current_identity]=authenticated_fixture
        async def fixture():
            db=Database(self.url)
            try:
                result=await DatabaseTicketStore(db).create('general','low','离线持久化样例','仅用于测试','api-owner')
                from smart_cs.db.models import User,AuthCredential
                async with db.sessions.begin() as s:
                    user=await s.get(User,'api-owner');user.role='admin'
                    s.add(AuthCredential(user_id='api-owner',login_name='api-owner',password_hash='explicit-fixture-not-for-login',enabled=True))
                return result
            finally:await db.close()
        ticket_id=asyncio.run(fixture())['ticket_id']
        route = '{"primary_intent":"transaction","confidence":0.98,"suggested_agent":"ticket_handler"}'
        with self.client([route, '{"action":{"name":"ticket_create","arguments":{"type":"refund","summary":"登记申请"}}}', '{"decision":"allow","risk_level":"low","reason_codes":["CONTENT_ALLOWED"]}']) as client:
            response = client.post('/api/chat/stream', json={'message': '请帮我建退款申请工单', 'user_id': 'api-owner'})
            final = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')][-1]
            self.assertEqual(final['type'], 'complete')
            self.assertNotIn('ticket_id',final['execution_trace'][2]['summary'])
            self.assertIn('不会创建工单',final['response'])
            self.assertEqual(final['action_plan'],{})
            self.assertEqual(final['action_review']['decision'],'clarify')
            self.assertFalse(final['action_review']['execution_authorized'])
            self.assertEqual(client.get('/api/foundation').json()['counts']['tickets'], 1)
            self.assertEqual(client.get('/api/tickets/' + ticket_id, params={'user_id': 'other-owner'}).status_code, 404)
        with self.client(['unused']) as client:
            result = client.get('/api/tickets/' + ticket_id, params={'user_id': 'api-owner'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['ticket']['status'], 'created')
            self.assertEqual(len(client.get('/api/tickets', params={'user_id': 'api-owner'}).json()['tickets']), 1)
            with patch.dict(os.environ, {'APP_MODE': 'production'}):
                self.assertEqual(client.get('/api/foundation').status_code, 503)
                self.assertEqual(client.post('/api/chat', json={'message': 'test'}).status_code, 503)


if __name__ == '__main__':
    unittest.main()
