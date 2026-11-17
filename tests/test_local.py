from legacy_identity import install_offline_identity
"""Offline business checks for the complete four-agent workflow."""
import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from pydantic import Field

import smart_cs.api.main as main
from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.agents.ticket_handler import TicketStore
from smart_cs.memory.long_term import LongTermMemory
from smart_cs.memory.short_term import ShortTermMemory
from smart_cs.memory.working_memory import WorkingMemory


PASSED = '{"passed":true,"risk_level":"low"}'
KNOWLEDGE_PATH = ['intent_router', 'supervisor_route', 'knowledge_rag', 'compliance_check', 'synthesize']
TICKET_PATH = ['intent_router', 'supervisor_route', 'ticket_handler', 'compliance_check', 'synthesize']
REVIEW_PATH = ['intent_router', 'supervisor_route', 'compliance_check', 'synthesize']


def classified(route='knowledge_rag', primary='consultation', secondary='refund_policy', confidence=0.95, entities=None):
    return json.dumps({'primary_intent': primary, 'secondary_intent': secondary,
                       'confidence': confidence, 'entities': entities or {}, 'suggested_agent': route}, ensure_ascii=False)


class RecordingModel(FakeListChatModel):
    calls: list = Field(default_factory=list)

    async def ainvoke(self, input, config=None, **kwargs):
        self.calls.append(list(input))
        return await super().ainvoke(input, config=config, **kwargs)


class LocalIntegrationTests(unittest.TestCase):
    def setUp(self):
        install_offline_identity(self, main.app)
        database_patch = patch.object(main, 'configured_database', return_value=None)
        database_patch.start()
        self.addCleanup(database_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def client(self, responses):
        self.fake = RecordingModel(responses=responses)
        self.working = WorkingMemory()
        def build(**kwargs):
            # Explicit offline baseline adapter. Live APIs inject a read-only port.
            kwargs['ticket_store']=TicketStore()
            return create_supervisor_graph(llm=self.fake,**kwargs)
        patches = [
            patch.object(main, 'graph', None),
            patch.object(main, 'ENV_PATH', Path(self.temp.name) / '.env'),
            patch.object(main, 'create_supervisor_graph', side_effect=build),
            patch.object(main, 'working_memory', self.working),
            patch.object(main, 'short_term_memory', ShortTermMemory('memory://')),
            patch.object(main, 'long_term_memory', LongTermMemory(self.temp.name + '/index')),
            patch.object(main, 'init_tracer'),
            patch.dict(os.environ, {'OPENAI_API_KEY': 'local-test-placeholder'}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return TestClient(main.app)

    def test_knowledge_answer_classification_path_and_history(self):
        answer = '购买后7天内可申请退款，3-5个工作日原路退回。[refund_policy.md]'
        with self.client([classified(entities={'policy': 'refund'}), '退款政策', '0,1,2', answer, PASSED]) as client:
            reply = client.post('/api/chat', json={'message': '退款政策是什么？'})
            self.assertEqual(reply.status_code, 200, reply.text)
            data = reply.json()
            self.assertEqual(data['response'], answer)
            self.assertTrue(data['compliance_passed'])
            self.assertEqual(data['agent_steps'], KNOWLEDGE_PATH)
            self.assertEqual(data['classification']['entities'], {'policy': 'refund'})
            self.assertEqual(len(self.fake.calls), 5)  # Supervisor must not call the model a second time.
            self.assertIn('"policy": "refund"', self.fake.calls[1][-1].content)
            context = self.working.get_context(data['session_id'])
            self.assertEqual(context['intent_details']['secondary'], 'refund_policy')
            history = client.get('/api/history/' + data['session_id']).json()['messages']
            self.assertEqual([m['role'] for m in history], ['user', 'assistant'])
            self.assertEqual(history[-1]['content'], answer)

    def test_ticket_query_uses_classifier_entities_and_conversation_context(self):
        responses = [classified('ticket_handler', 'transaction', 'ticket_create'),
                     '```json\n{"action":"create","ticket_type":"refund","summary":"申请退款"}\n```', PASSED,
                     '{}', '{"action":"query"}', PASSED]
        with self.client(responses) as client:
            first = client.post('/api/chat', json={'message': '请帮我申请退款', 'user_id': 'local_test'})
            self.assertEqual(first.status_code, 200, first.text)
            data = first.json()
            self.assertIn('类型: refund', data['response'])
            ticket_id = re.search(r'TK-\d{8}-[A-F0-9]{6}', data['response']).group()
            self.fake.responses[3] = '```json\n' + classified('ticket_handler', 'consultation', 'ticket_query', entities={'ticket_id': ticket_id}) + '\n```'
            second = client.post('/api/chat', json={
                'message': '刚才那张工单现在是什么状态？', 'user_id': 'local_test', 'session_id': data['session_id'],
            })
            self.assertEqual(second.status_code, 200, second.text)
            self.assertIn('工单查询结果', second.json()['response'])
            self.assertIn(ticket_id, second.json()['response'])
            self.assertEqual(second.json()['agent_steps'], TICKET_PATH)
            self.assertEqual(second.json()['classification']['entities']['ticket_id'], ticket_id)
            self.assertIn(ticket_id, '\n'.join(m.content for m in self.fake.calls[3]))
            self.assertIn(ticket_id, self.fake.calls[4][-1].content)
            self.assertEqual(len(client.get('/api/history/' + data['session_id']).json()['messages']), 4)

    def test_noncompliant_answer_is_blocked_without_fake_handoff(self):
        with self.client([classified(), '理财产品', '0', '该产品保证收益。']) as client:
            reply = client.post('/api/chat', json={'message': '产品收益如何？'})
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertFalse(reply.json()['compliance_passed'])
            self.assertNotIn('保证收益', reply.json()['response'])
            self.assertNotIn('工单编号已自动生成', reply.json()['response'])
            self.assertNotIn('已转交', reply.json()['response'])
            self.assertEqual(reply.json()['agent_steps'], KNOWLEDGE_PATH)

    def test_fenced_compliance_verdict_is_respected(self):
        with self.client([classified(), '退款政策', '0', '您的申请需要核实。',
                          '```json\n{"passed":false,"risk_level":"high","violations":["需要人工核实"]}\n```']) as client:
            reply = client.post('/api/chat', json={'message': '政策是什么？'})
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertFalse(reply.json()['compliance_passed'])

    def test_direct_risk_consultation_generates_and_reviews_an_answer(self):
        answer = '请通过平台官方渠道核实账户状态，不要向他人提供验证码。'
        with self.client([classified('compliance_checker', 'compliance', 'security_consultation'), answer, PASSED]) as client:
            reply = client.post('/api/chat', json={'message': '有人索要验证码，我怀疑账户异常。'})
            self.assertEqual(reply.status_code, 200, reply.text)
            data = reply.json()
            self.assertEqual(data['response'], answer)
            self.assertTrue(data['compliance_passed'])
            self.assertEqual(data['agent_steps'], REVIEW_PATH)
            self.assertEqual(len(self.fake.calls), 3)

    def test_low_confidence_requests_clarification_instead_of_creating_a_ticket(self):
        with self.client([classified('ticket_handler', 'transaction', 'ticket_create', confidence=0.2), PASSED]) as client:
            reply = client.post('/api/chat', json={'message': '帮我处理一下'})
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(reply.json()['intent'], 'clarification')
            self.assertIn('请补充具体需求', reply.json()['response'])
            self.assertNotIn('TK-', reply.json()['response'])
            self.assertEqual(reply.json()['agent_steps'], REVIEW_PATH)
            self.assertEqual(len(self.fake.calls), 2)

    def test_invalid_classification_cannot_crash_or_trigger_a_transaction(self):
        values = ['not json', classified(primary='unsupported'), classified(confidence='NaN'),
                  classified('nonexistent_agent'), classified(confidence={'invalid': 1})]
        for value in values:
            with self.subTest(value=value):
                with self.client([value, PASSED]) as client:
                    reply = client.post('/api/chat', json={'message': '帮我处理一下'})
                    self.assertEqual(reply.status_code, 200, reply.text)
                    self.assertEqual(reply.json()['intent'], 'clarification')
                    self.assertNotIn('ticket_handler', reply.json()['agent_steps'])

    def test_invalid_compliance_json_does_not_silently_approve(self):
        for verdict in ['not json', '{"passed":"true","risk_level":"low"}', '{"passed":true,"risk_level":[]}']:
            with self.subTest(verdict=verdict):
                with self.client([classified(), '退款政策', '0', '退款需按公布的政策办理。', verdict]) as client:
                    reply = client.post('/api/chat', json={'message': '退款政策是什么？'})
                    self.assertEqual(reply.status_code, 200, reply.text)
                    self.assertFalse(reply.json()['compliance_passed'])

    def test_query_without_an_identifier_does_not_create_a_ticket(self):
        with self.client([classified('ticket_handler', 'consultation', 'ticket_query'), '{"action":"query"}', PASSED]) as client:
            reply = client.post('/api/chat', json={'message': '帮我查工单'})
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertIn('请提供完整工单号', reply.json()['response'])
            self.assertNotIn('创建成功', reply.json()['response'])

    def test_invalid_ticket_analysis_does_not_create_a_generic_ticket(self):
        with self.client([classified('ticket_handler', 'transaction', 'ticket_create'), 'not json', PASSED]) as client:
            reply = client.post('/api/chat', json={'message': '请帮我申请退款'})
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertNotIn('创建成功', reply.json()['response'])
            self.assertNotIn('TK-', reply.json()['response'])

    def test_actual_graph_events_include_the_intent_router(self):
        fake = RecordingModel(responses=[classified(), '退款政策', '0', '退款政策见文档。', PASSED])
        memory = LongTermMemory(self.temp.name + '/stream-index')
        memory.add_document('购买后7天内可申请退款。', 'refund_policy.md')
        graph = create_supervisor_graph(llm=fake, long_term_memory=memory)
        state = {'messages': [HumanMessage(content='退款政策是什么？')], 'user_id': 'test', 'session_id': 'stream',
                 'intent': '', 'intent_details': {}, 'agent_steps': [], 'sub_results': {}, 'compliance_passed': True,
                 'final_response': '', 'current_agent': '', 'retry_count': 0}

        async def events():
            return [next(iter(update)) async for update in graph.astream(state, {'configurable': {'thread_id': 'stream'}}, stream_mode='updates')]

        self.assertEqual(asyncio.run(events()), KNOWLEDGE_PATH)

    def test_tools_health_and_validation(self):
        with self.client([classified()]) as client:
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertEqual(client.get('/docs').status_code, 200)
            names = {t['name'] for t in client.get('/api/tools').json()['tools']}
            self.assertIn('order_query', names)
            # Direct tools must not fabricate sample orders when no database exists.
            order = client.post('/api/tools/call', json={'name': 'order_query', 'arguments': {'order_id': 'LOCAL-001'}})
            self.assertEqual(order.status_code,503)
            self.assertEqual(client.post('/api/tools/call', json={'name': 'missing', 'arguments': {}}).status_code,403)
            self.assertEqual(client.post('/api/chat', json={}).status_code, 422)

    def test_model_settings_are_used(self):
        fake = RecordingModel(responses=[classified()])
        with patch.dict(os.environ, {'MODEL_NAME': 'provider-test-model', 'OPENAI_BASE_URL': 'https://example.invalid/v1', 'OPENAI_API_KEY': 'test-key'}, clear=True):
            with patch('smart_cs.models.ChatOpenAI', return_value=fake) as constructor:
                create_supervisor_graph(long_term_memory=LongTermMemory(self.temp.name + '/config'))
                self.assertEqual(constructor.call_args.kwargs['model'], 'provider-test-model')
                self.assertEqual(constructor.call_args.kwargs['base_url'], 'https://example.invalid/v1')

    def test_unconfigured_service_is_explicit_and_can_load_saved_settings(self):
        fake = RecordingModel(responses=[classified()])
        settings = Path(self.temp.name) / '.env'
        settings.write_text('OPENAI_API_KEY=\n', encoding='utf-8')
        with patch.object(main, 'graph', None), patch.object(main, 'ENV_PATH', settings):
            with patch.object(main, 'init_tracer'), patch.object(main, 'create_supervisor_graph', return_value=fake) as build:
                with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
                    with TestClient(main.app) as client:
                        self.assertFalse(client.get('/health').json()['chat_ready'])
                        self.assertEqual(client.get('/docs').status_code, 200)
                        self.assertEqual(client.post('/api/chat', json={'message': 'test'}).status_code, 503)
                        build.assert_not_called()
                        settings.write_text('OPENAI_API_KEY=local-test-placeholder\n', encoding='utf-8')
                        main.initialize_graph()
                        build.assert_called_once()
                        self.assertTrue(client.get('/health').json()['chat_ready'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
