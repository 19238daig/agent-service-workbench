from legacy_identity import install_offline_identity, override_identity
"""Verify real LangGraph events, branch selection and the public trace boundary."""
import json
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel

import smart_cs.api.main as main
from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.memory.long_term import LongTermMemory
from smart_cs.memory.short_term import ShortTermMemory
from smart_cs.workflow import node_summary


def classify(route='knowledge_rag', primary='consultation', confidence=0.95):
    return json.dumps({'primary_intent': primary, 'secondary_intent': 'refund',
                       'confidence': confidence, 'suggested_agent': route, 'entities': {}})


PASS = '{"passed":true,"risk_level":"low"}'


@contextmanager
def client_for(responses, model=None):
    with tempfile.TemporaryDirectory() as directory:
        memory = LongTermMemory(directory + '/index')
        workflow = create_supervisor_graph(llm=model or FakeListChatModel(responses=responses), long_term_memory=memory)
        short = ShortTermMemory('memory://')
        with patch.object(main, 'graph', workflow), patch.object(main, 'initialize_graph'), \
             patch.object(main, 'init_tracer'), patch.object(main, 'long_term_memory', memory), \
             patch.object(main, 'short_term_memory', short):
            with override_identity(main.app), TestClient(main.app) as client:
                yield client


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        install_offline_identity(self, main.app)
        database_patch = patch.object(main, 'configured_database', return_value=None)
        database_patch.start()
        self.addCleanup(database_patch.stop)

    def test_progress_projection_excludes_unvalidated_model_strings(self):
        summary = node_summary('ticket_handler', {'ticket_evidence': {'action': 'query', 'ticket_id': 'PRIVATE_MODEL_TEXT', 'found': False}})
        self.assertNotIn('PRIVATE_MODEL_TEXT', json.dumps(summary))
        summary = node_summary('intent_router', {'intent_details': {'primary': 'consultation', 'secondary': 'PRIVATE_MODEL_TEXT', 'confidence': 0.95}})
        self.assertNotIn('PRIVATE_MODEL_TEXT', json.dumps(summary))

    def test_four_routes_emit_actual_order_models_duration_and_persist_history(self):
        scenarios = [
            ([classify(), '退款', '0,1,2', '7天内可申请退款。[refund_policy.md]', PASS], 'knowledge_rag'),
            ([classify('ticket_handler', 'transaction'), '{"action":"create","summary":"退款"}', PASS], 'ticket_handler'),
            ([classify('compliance_checker', 'compliance'), '请通过平台官方渠道核实账户异常。', PASS], None),
            ([classify(primary='unknown', confidence=0.1), PASS], None),
        ]
        for responses, branch in scenarios:
            with self.subTest(branch=branch), client_for(responses) as client:
                profile = client.get('/api/workflow').json()
                self.assertEqual(len([n for n in profile['nodes'] if n.get('role')]), 4)
                self.assertTrue(all(n['model'] == 'FakeListChatModel' for n in profile['nodes'] if n.get('role')))
                self.assertNotIn('api_key', json.dumps(profile))
                self.assertTrue(any(e['source'] == 'supervisor_route' and e['conditional'] for e in profile['edges']))
                response = client.post('/api/chat/stream', json={'message': '测试业务需求'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('text/event-stream', response.headers['content-type'])
                data = events(response)
                expected = ['intent_router', 'supervisor_route', *([branch] if branch else []), 'compliance_check', 'synthesize']
                self.assertEqual([e['node'] for e in data if e['type'] == 'node_start'], expected)
                self.assertEqual([e['node'] for e in data if e['type'] == 'node_end'], expected)
                self.assertEqual(data[0]['type'], 'session')
                self.assertEqual(data[-1]['type'], 'complete')
                final = data[-1]
                self.assertEqual(final['agent_steps'], expected)
                self.assertEqual([s['node'] for s in final['execution_trace']], expected)
                self.assertGreater(final['duration_ms'], 0)
                self.assertTrue(all(s['duration_ms'] >= 0 for s in final['execution_trace']))
                if branch == 'knowledge_rag':
                    summary = final['execution_trace'][2]['summary']
                    self.assertGreater(summary['retrieved_count'], 0)
                    self.assertTrue(summary['sources'])
                if branch == 'ticket_handler':
                    summary = final['execution_trace'][2]['summary']
                    self.assertTrue(summary['found'])
                    self.assertRegex(summary['ticket_id'], r'^TK-')
                history = client.get('/api/history/' + final['session_id']).json()['messages']
                self.assertEqual([m['role'] for m in history], ['user', 'assistant'])

    def test_rejected_draft_and_private_model_content_never_leak_in_progress(self):
        draft = 'PRIVATE_UNAUDITED_DRAFT'
        with client_for([classify(), '退款', '0', draft,
                         '{"passed":false,"risk_level":"high","violations":["PRIVATE_UNAUDITED_DRAFT"]}']) as client:
            response = client.post('/api/chat/stream', json={'message': '退款政策'})
            self.assertNotIn(draft, response.text)
            data = events(response)
            review = next(e for e in data if e['type'] == 'node_end' and e['node'] == 'compliance_check')
            self.assertEqual(review['status'], 'blocked')
            self.assertFalse(data[-1]['compliance_passed'])
            self.assertIn('需要进一步核实', data[-1]['response'])

    def test_stream_errors_are_safe_and_do_not_claim_completion(self):
        class BrokenModel(FakeListChatModel):
            async def ainvoke(self, *args, **kwargs):
                raise RuntimeError('DO_NOT_PUBLISH_PROVIDER_SECRET')
        with client_for([], BrokenModel(responses=['unused'])) as client:
            response = client.post('/api/chat/stream', json={'message': '退款政策'})
            data = events(response)
            self.assertEqual(data[-1]['type'], 'error')
            self.assertFalse(any(e['type'] == 'complete' for e in data))
            self.assertNotIn('DO_NOT_PUBLISH_PROVIDER_SECRET', response.text)
            history = client.get('/api/history/' + data[0]['session_id']).json()['messages']
            self.assertEqual([m['role'] for m in history], ['user'])

    def test_unconfigured_stream_returns_503_before_starting(self):
        with patch.object(main, 'graph', None), patch.object(main, 'initialize_graph'), patch.object(main, 'init_tracer'):
            with override_identity(main.app), TestClient(main.app) as client:
                self.assertEqual(client.post('/api/chat/stream', json={'message': '退款'}).status_code, 503)
                self.assertFalse(client.get('/api/workflow').json()['ready'])


if __name__ == '__main__':
    unittest.main()
