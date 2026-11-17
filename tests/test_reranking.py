"""Cross-encoder contracts and real PostgreSQL revalidation; fixtures are not quality scores."""
import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from smart_cs.db.models import KnowledgeDocument
from smart_cs.retrieval.reranking import RerankConfig, CrossEncoderReranker, digest, validate_identity
from smart_cs.schemas.errors import ApplicationError
import test_hybrid_retrieval as hybrid_fixture


CONTRACT = {'protocol': 'bge-cross-encoder-v2', 'model': 'bge-reranker-v2-m3',
            'files_sha256': {'model.safetensors': 'a'*64}, 'normalization': 'sigmoid-float32-logits',
            'local_files_only': True, 'trust_remote_code': False}
IDENTITY = {'sha256': digest(CONTRACT), 'contract': CONTRACT}


def config(**changes):
    return RerankConfig(expected_identity=IDENTITY['sha256'], **changes)


def document(identifier='a'):
    return {'id': identifier, 'content': '测试条款 '+identifier, 'source': 'fixture.txt',
            'score': .7, 'metadata': {'heading': '章节', 'index_id': 'fixture'}}


def handler(scores=None, change=None, after=None):
    posts = []
    def respond(request):
        if request.url.path == '/identity':
            value = after if posts and after else IDENTITY
            return httpx.Response(200, json=value)
        body = json.loads(request.content); posts.append(body)
        values = scores if scores is not None else [.8]*len(body['passages'])
        result = {'scores': values, 'identity_sha256': IDENTITY['sha256'], 'normalized': True,
                  'max_length': body['max_length'], 'batch_size': body['batch_size'],
                  'pairs': len(body['passages']), 'input_pair_tokens_before_truncation': [100]*len(body['passages']),
                  'truncated_pairs': 0, 'duration_ms': 1.}
        if change:
            result.update(change)
        return httpx.Response(200, json=result)
    return respond, posts


class RerankContractTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, scores=None, change=None, after=None, **changes):
        respond, self.posts = handler(scores, change, after)
        return CrossEncoderReranker(config(**changes), transport=httpx.MockTransport(respond))

    async def test_bounds_reject_nan_coercion_excess_context_and_unknown_settings(self):
        for values in [{'candidate_k': True}, {'candidate_k': 51}, {'context_k': 21}, {'max_length': 1024},
                       {'batch_size': 8}, {'batch_size': True}, {'max_length': 256.0},
                       {'min_score': float('nan')}, {'enabled': 'false'}, {'owner': 'bob'}]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                config(**values)

    async def test_identity_is_bound_to_content_and_expected_model(self):
        self.assertEqual(validate_identity(IDENTITY), IDENTITY)
        bad = copy.deepcopy(IDENTITY); bad['contract']['model'] = 'other'
        with self.assertRaisesRegex(ApplicationError, 'RERANK_IDENTITY_INVALID'):
            validate_identity(bad)
        with self.assertRaisesRegex(ApplicationError, 'RERANK_IDENTITY_CHANGED'):
            validate_identity(IDENTITY, 'b'*64)

    async def test_identity_change_after_inference_fails_closed(self):
        other = copy.deepcopy(IDENTITY); other['contract']['revision'] = 'changed'; other['sha256'] = digest(other['contract'])
        with self.assertRaisesRegex(ApplicationError, 'RERANK_IDENTITY_CHANGED'):
            await self.adapter(after=other).rank('问题', [document()], revalidate=AsyncMock())

    async def test_stable_ties_deduplicate_and_preserve_canonical_fields(self):
        a, b = document('a'), document('b'); check = AsyncMock()
        result = await self.adapter().rank('问题', [b, a, a], revalidate=check)
        self.assertEqual([d['id'] for d in result['documents']], ['a', 'b'])
        self.assertEqual(result['documents'][0]['score'], .7)
        self.assertEqual(result['documents'][0]['metadata'], a['metadata'])
        self.assertEqual(len(self.posts[0]['passages']), 2); self.assertEqual(check.await_count, 2)
        self.assertFalse(result['audit']['scores_are_confidence'])

    async def test_duplicate_with_conflicting_content_is_rejected(self):
        adapter = self.adapter(); a = document()
        with self.assertRaisesRegex(ApplicationError, 'RERANK_DUPLICATE_CONFLICT'):
            await adapter.rank('问题', [a, {**a, 'content': '伪造'}], revalidate=AsyncMock())
        self.assertEqual(self.posts, [])

    async def test_invalid_scores_never_become_evidence(self):
        for scores in [[True], [-.1], [1.1], [], ['.9']]:
            with self.subTest(scores=scores), self.assertRaisesRegex(ApplicationError, 'RERANK_RESPONSE_INVALID'):
                await self.adapter(scores=scores).rank('问题', [document()], revalidate=AsyncMock())

    async def test_invalid_response_provenance_and_truncation_are_rejected(self):
        for changes in [{'identity_sha256': 'x'*64}, {'normalized': False}, {'max_length': 256},
                        {'pairs': True}, {'truncated_pairs': 1}, {'input_pair_tokens_before_truncation': [0]}]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ApplicationError, 'RERANK_RESPONSE_INVALID'):
                await self.adapter(change=changes).rank('问题', [document()], revalidate=AsyncMock())

    async def test_candidate_context_and_threshold_budgets(self):
        adapter = self.adapter(scores=[.09, .8, .7], candidate_k=3, context_k=1)
        result = await adapter.rank('问题', [document(str(i)) for i in range(5)], revalidate=AsyncMock())
        self.assertEqual(len(self.posts[0]['passages']), 3)
        self.assertEqual([d['id'] for d in result['documents']], ['1'])
        self.assertEqual(len(result['ranked_candidates']), 3)

    async def test_revalidation_before_inference_blocks_all_model_calls(self):
        adapter = self.adapter()
        with self.assertRaisesRegex(ApplicationError, 'DENIED'):
            await adapter.rank('问题', [document()], revalidate=AsyncMock(side_effect=ApplicationError('DENIED', 'denied', 403)))
        self.assertEqual(self.posts, [])

    async def test_revalidation_after_inference_blocks_publication(self):
        check = AsyncMock(side_effect=[None, ApplicationError('REVOKED', 'revoked', 409)])
        with self.assertRaisesRegex(ApplicationError, 'REVOKED'):
            await self.adapter().rank('问题', [document()], revalidate=check)

    async def test_bypass_skips_model_but_still_checks_permission_and_budget(self):
        adapter = self.adapter(enabled=False, context_k=1); check = AsyncMock()
        result = await adapter.rank('问题', [document('z'), document('a')], revalidate=check)
        self.assertEqual([d['id'] for d in result['documents']], ['z'])
        self.assertEqual(self.posts, []); self.assertEqual(check.await_count, 2)

    async def test_empty_evidence_does_not_call_model(self):
        adapter = self.adapter(); result = await adapter.rank('问题', [], revalidate=AsyncMock())
        self.assertEqual(result['documents'], []); self.assertEqual(self.posts, [])

    async def test_timeout_never_falls_back_to_unranked_answer(self):
        def timeout(request): raise httpx.ReadTimeout('fixture')
        adapter = CrossEncoderReranker(config(), transport=httpx.MockTransport(timeout))
        with self.assertRaisesRegex(ApplicationError, 'RERANK_TIMEOUT'):
            await adapter.rank('问题', [document()], revalidate=AsyncMock())

    async def test_server_error_never_silently_disables_reranking(self):
        adapter = CrossEncoderReranker(config(), transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        with self.assertRaisesRegex(ApplicationError, 'RERANK_UNAVAILABLE'):
            await adapter.rank('问题', [document()], revalidate=AsyncMock())

    async def test_invalid_environment_config_is_not_coerced(self):
        for name, value in [('RAG_RERANK_ENABLED', 'yes'), ('RAG_RERANK_MIN_SCORE', 'nan')]:
            with patch.dict(os.environ, {name: value}), self.assertRaisesRegex(ApplicationError, 'RERANK_CONFIG_INVALID'):
                RerankConfig.deployed()


class RerankDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only disposable-schema lifecycle, without inheriting its test cases.
    setUpClass = classmethod(hybrid_fixture.HybridDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(hybrid_fixture.HybridDatabaseTests.tearDownClass.__func__)
    asyncSetUp = hybrid_fixture.HybridDatabaseTests.asyncSetUp
    asyncTearDown = hybrid_fixture.HybridDatabaseTests.asyncTearDown
    add = hybrid_fixture.HybridDatabaseTests.add

    async def lookup(self, identity=None):
        return (await self.hybrid.search('退款规则', identity=identity))['documents']

    async def rank(self, documents, respond=None):
        if respond is None: respond, _ = handler()
        return await CrossEncoderReranker(config(), transport=httpx.MockTransport(respond)).rank(
            '退款规则', documents, revalidate=self.hybrid.dense.revalidate)

    async def test_another_users_candidate_is_blocked_before_model(self):
        await self.add(scope='private', owner='bob'); await self.builder.build(activate=True)
        docs = await self.lookup(self.bob); respond, posts = handler()
        with self.assertRaisesRegex(ApplicationError, 'RETRIEVAL_AUTHORIZATION_CHANGED'):
            await self.rank(docs, respond)
        self.assertFalse(posts)

    async def test_withdrawal_during_inference_is_not_published(self):
        identifier = await self.add(); await self.builder.build(activate=True); docs = await self.lookup()
        responder, _ = handler()
        async def revoke(request):
            if request.url.path == '/rerank/v2':
                async with self.db.sessions.begin() as s:
                    (await s.get(KnowledgeDocument, identifier)).status = 'withdrawn'
            return responder(request)
        with self.assertRaisesRegex(ApplicationError, 'RETRIEVAL_AUTHORIZATION_CHANGED'):
            await self.rank(docs, revoke)

    async def test_index_switch_during_inference_is_not_published(self):
        await self.add(); await self.builder.build(activate=True); docs = await self.lookup(); responder, _ = handler()
        async def switch(request):
            if request.url.path == '/rerank/v2':
                await self.add('新资料'); await self.builder.build(activate=True)
            return responder(request)
        with self.assertRaisesRegex(ApplicationError, 'RETRIEVAL_AUTHORIZATION_CHANGED'):
            await self.rank(docs, switch)

    async def test_empty_or_bypass_never_restores_withdrawn_evidence(self):
        identifier = await self.add(); await self.builder.build(activate=True); docs = await self.lookup()
        async with self.db.sessions.begin() as s:
            (await s.get(KnowledgeDocument, identifier)).status = 'withdrawn'
        with self.assertRaisesRegex(ApplicationError, 'RETRIEVAL_AUTHORIZATION_CHANGED'):
            await CrossEncoderReranker(config(enabled=False)).rank('问题', docs, revalidate=self.hybrid.dense.revalidate)

    async def test_rerank_profile_change_invalidates_task_context(self):
        from smart_cs.persistence.conversations import context_fingerprint
        async with self.db.sessions() as s:
            with patch.dict(os.environ, {'RAG_RERANK_ENABLED': 'true'}): a = await context_fingerprint(s, self.alice)
            with patch.dict(os.environ, {'RAG_RERANK_ENABLED': 'false'}): b = await context_fingerprint(s, self.alice)
            with patch.dict(os.environ, {'RAG_RERANK_ENABLED': 'true'}): c = await context_fingerprint(s, self.alice)
        self.assertNotEqual(a, b); self.assertEqual(a, c)

    async def test_canonical_chunk_and_quote_inputs_survive_scoring(self):
        await self.add(); await self.builder.build(activate=True); docs = await self.lookup()
        result = await self.rank(docs)
        self.assertEqual(result['documents'][0]['content'], docs[0]['content'])
        self.assertEqual(result['documents'][0]['metadata'], docs[0]['metadata'])
