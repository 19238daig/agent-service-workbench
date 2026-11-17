"""Serving budgets, signed evidence isolation and real Redis/PG checks."""
import asyncio
import copy
import json
import os
import unittest
from unittest.mock import patch,AsyncMock
from uuid import uuid4
import httpx
from sqlalchemy import select
from smart_cs.observability.context import RunObservation,run_context
from smart_cs.observability.models import observe
from smart_cs.performance.policy import ServingPolicy
from smart_cs.performance.rewrite import needs_rewrite
from smart_cs.performance.runtime import summary
from smart_cs.performance.cache import EvidenceCache,RedisRanks
from smart_cs.schemas.errors import ApplicationError
from smart_cs.db.models import KnowledgeDocument
from smart_cs.services.access import principal_context
import test_hybrid_retrieval as fixtures


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.observation=RunObservation('fixture','fixture','fixture');self.token=run_context.set(self.observation)
    async def asyncTearDown(self):run_context.reset(self.token)

    async def call(self,function,policy):
        with patch.object(ServingPolicy,'deployed',return_value=policy):return await observe(function,'fixture',role='knowledge_rag')

    async def test_rewrite_skips_only_self_contained_history_question(self):
        self.assertFalse(needs_rewrite('自动续费前是否需要明确提醒消费者？',['history']))
        self.assertTrue(needs_rewrite('那这个要怎么办？',['history']))
        self.assertTrue(needs_rewrite('七天以后再进行办理还来得及吗',['history']))
        self.assertFalse(needs_rewrite('退款规则',[]))
        with patch.object(ServingPolicy,'deployed',return_value=ServingPolicy(enabled=False)):
            self.assertTrue(needs_rewrite('自动续费前是否需要明确提醒消费者？',['history']))

    async def test_timeout_is_recorded_once_and_never_replayed(self):
        calls=0
        async def slow():
            nonlocal calls;calls+=1;await asyncio.sleep(.1)
        with self.assertRaises(TimeoutError):await self.call(slow,ServingPolicy(call_seconds=.01))
        self.assertEqual(calls,1);self.assertEqual(len(self.observation.calls),1)
        self.assertEqual(self.observation.calls[0]['status'],'failed')
        self.assertEqual(self.observation.calls[0]['error_code'],'MODEL_TIMEOUT')

    async def test_known_rate_limit_retry_has_two_separate_attempt_records(self):
        calls=0
        async def limited():
            nonlocal calls;calls+=1
            if calls==1:raise httpx.HTTPStatusError('fixture',request=httpx.Request('GET','http://localhost'),response=httpx.Response(429))
            return object()
        await self.call(limited,ServingPolicy())
        self.assertEqual([c['attempt'] for c in self.observation.calls],[1,2])
        self.assertEqual(summary(self.observation)['retries'],1)

    async def test_call_budget_blocks_next_model_without_hiding_unknown_usage(self):
        async def value():return object()
        policy=ServingPolicy(max_chat_attempts=1)
        await self.call(value,policy)
        with self.assertRaisesRegex(ApplicationError,'MODEL_CALL_BUDGET_EXCEEDED'):await self.call(value,policy)
        self.assertEqual(len(self.observation.calls),1)
        self.assertEqual(self.observation.calls[0]['usage']['status'],'unknown')

    async def test_reported_token_budget_blocks_further_calls(self):
        self.observation.calls=[{'kind':'chat','usage':{'status':'available','total_tokens':10}}]
        with self.assertRaisesRegex(ApplicationError,'MODEL_TOKEN_BUDGET_EXCEEDED'):
            await self.call(AsyncMock(),ServingPolicy(reported_token_budget=10))

    async def test_round_budget_counts_time_before_model_call(self):
        self.observation.started-=10
        with self.assertRaisesRegex(ApplicationError,'ROUND_BUDGET_EXCEEDED'):
            await self.call(AsyncMock(),ServingPolicy(round_seconds=1.))

    async def test_concurrency_limit_and_queue_timeout_do_not_invoke_waiting_work(self):
        entered=asyncio.Event();release=asyncio.Event();calls=0
        async def hold():
            nonlocal calls;calls+=1;entered.set();await release.wait()
        policy=ServingPolicy(global_concurrency=1,queue_seconds=.02)
        with patch.object(ServingPolicy,'deployed',return_value=policy):
            first=asyncio.create_task(observe(hold,'fixture',role='knowledge_rag'));await entered.wait()
            try:
                with self.assertRaisesRegex(ApplicationError,'MODEL_QUEUE_TIMEOUT'):await observe(hold,'fixture',role='knowledge_rag')
            finally:release.set();await first
        self.assertEqual(calls,1)

    async def test_cancellation_releases_semaphore_for_next_request(self):
        started=asyncio.Event()
        async def hold():started.set();await asyncio.sleep(10)
        with patch.object(ServingPolicy,'deployed',return_value=ServingPolicy(global_concurrency=1)):
            first=asyncio.create_task(observe(hold,'fixture'));await started.wait();first.cancel()
            with self.assertRaises(asyncio.CancelledError):await first
            await observe(AsyncMock(return_value=object()),'fixture')
        self.assertEqual(self.observation.calls[0]['status'],'cancelled')

    async def test_authentication_and_invalid_output_are_not_retried(self):
        calls=AsyncMock(side_effect=ValueError('fixture'))
        with self.assertRaises(ValueError):await self.call(calls,ServingPolicy())
        self.assertEqual(calls.await_count,1)


class RedisTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_redis_ttl_capacity_and_private_namespace(self):
        if not os.getenv('REDIS_URL'):self.skipTest('Real Redis required')
        from redis.asyncio import Redis
        prefix='smartcs:perf:test-'+uuid4().hex;keys=[prefix+':'+str(i) for i in range(3)]
        redis=Redis.from_url(os.environ['REDIS_URL']);backend=RedisRanks()
        try:
            for key in keys:await backend.write(key,b'fixture',2,2)
            self.assertIsNone(await backend.read(keys[0]));self.assertEqual(await backend.read(keys[2]),b'fixture')
            self.assertGreater(await redis.ttl(keys[2]),0)
            await backend.write(keys[2],b'short',1,2);await asyncio.sleep(1.1)
            self.assertIsNone(await backend.read(keys[2]))
        finally:await redis.delete(*keys,prefix+':entries');await redis.aclose()


class MapRanks:
    def __init__(self):self.values={};self.error=False;self.on_read=None
    async def read(self,key):
        if self.error:raise ConnectionError('fixture')
        if self.on_read:await self.on_read()
        return self.values.get(key)
    async def write(self,key,value,ttl,limit):
        if self.error:raise ConnectionError('fixture')
        self.values[key]=value


class CacheDatabaseTests(unittest.IsolatedAsyncioTestCase):
    setUpClass=classmethod(fixtures.HybridDatabaseTests.setUpClass.__func__)
    tearDownClass=classmethod(fixtures.HybridDatabaseTests.tearDownClass.__func__)
    asyncSetUp=fixtures.HybridDatabaseTests.asyncSetUp
    asyncTearDown=fixtures.HybridDatabaseTests.asyncTearDown
    add=fixtures.HybridDatabaseTests.add

    async def initialize(self):
        await self.add('PUBLIC_CACHE_BODY');await self.add('ALICE_PRIVATE_BODY',scope='private',owner='alice')
        await self.builder.build(activate=True);self.backend=MapRanks();self.loads=0
        self.cache=EvidenceCache(self.db,self.hybrid.dense,backend=self.backend,secret=b'x'*32)
    async def fetch(self,configuration=None):
        async def load():self.loads+=1;return (await self.hybrid.search('退款规则'))['documents']
        return await self.cache.get_or_load('search','PRIVATE_QUERY',configuration or {'mode':'hybrid'},load)

    async def test_hit_reconstructs_canonical_evidence_and_stores_no_text(self):
        await self.initialize();a=await self.fetch();b=await self.fetch()
        self.assertEqual(self.loads,1);self.assertEqual(a[0]['content'],b[0]['content'])
        raw=b''.join(self.backend.values.values());self.assertNotIn(b'PUBLIC_CACHE_BODY',raw);self.assertNotIn(b'PRIVATE_QUERY',raw)

    async def test_customer_and_role_keys_never_reuse_foreign_private_evidence(self):
        await self.initialize()
        token=principal_context.set(self.alice)
        try:alice=await self.fetch()
        finally:principal_context.reset(token)
        token=principal_context.set(self.bob)
        try:bob=await self.fetch()
        finally:principal_context.reset(token)
        self.assertEqual(self.loads,2);self.assertTrue(any('ALICE_PRIVATE' in d['content'] for d in alice))
        self.assertFalse(any('ALICE_PRIVATE' in d['content'] for d in bob))

    async def test_withdrawal_changes_scope_and_cannot_restore_old_evidence(self):
        await self.initialize();first=await self.fetch();identifier=first[0]['metadata']['document_id']
        async with self.db.sessions.begin() as s:(await s.get(KnowledgeDocument,identifier)).status='withdrawn'
        result=await self.fetch();self.assertEqual(result,[]);self.assertEqual(self.loads,2)

    async def test_tampered_rank_signature_is_miss_not_a_published_result(self):
        await self.initialize();original=await self.fetch();key=next(iter(self.backend.values))
        envelope=json.loads(self.backend.values[key]);envelope['body']['rows'][0]['id']='FOREIGN'
        self.backend.values[key]=json.dumps(envelope).encode();result=await self.fetch()
        self.assertEqual(self.loads,2);self.assertEqual(result[0]['id'],original[0]['id'])

    async def test_unavailable_redis_retrieves_fresh_evidence(self):
        await self.initialize();self.backend.error=True;result=await self.fetch()
        self.assertTrue(result);self.assertEqual(self.loads,1)

    async def test_model_switch_rejects_cached_old_model(self):
        await self.initialize();await self.fetch();self.models.name='stub@'+'b'*64
        with self.assertRaises(ApplicationError):await self.fetch()

    async def test_configuration_changes_never_reuse_other_budget_or_model_key(self):
        await self.initialize();await self.fetch({'context':3});await self.fetch({'context':5})
        self.assertEqual(self.loads,2)

    async def test_index_switch_invalidates_cached_membership(self):
        await self.initialize();first=await self.fetch();await self.add('NEW_POLICY');await self.builder.build(activate=True)
        second=await self.fetch();self.assertEqual(self.loads,2)
        self.assertNotEqual(first[0]['metadata']['index_id'],second[0]['metadata']['index_id'])

    async def test_revocation_between_lookup_and_hydration_fails_closed(self):
        await self.initialize();first=await self.fetch();identifier=first[0]['metadata']['document_id']
        async def revoke():
            async with self.db.sessions.begin() as s:(await s.get(KnowledgeDocument,identifier)).status='withdrawn'
        self.backend.on_read=revoke;result=await self.fetch();self.assertEqual(result,[])

    async def test_dashboard_requires_admin_and_exposes_no_source_body(self):
        import smart_cs.api.main as main
        from smart_cs.api.dependencies import current_identity
        with patch.object(main,'database',self.db):
            try:
                main.app.dependency_overrides[current_identity]=lambda:self.alice
                async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app),base_url='http://test') as client:
                    self.assertEqual((await client.get('/api/admin/performance')).status_code,403)
                    main.app.dependency_overrides[current_identity]=lambda:self.alice.model_copy(update={'role':'admin'})
                    response=await client.get('/api/admin/performance');self.assertEqual(response.status_code,200)
                    self.assertEqual(response.json()['runs'],0)
                    self.assertNotIn('PRIVATE_QUERY',response.text)
            finally:main.app.dependency_overrides.pop(current_identity,None)
