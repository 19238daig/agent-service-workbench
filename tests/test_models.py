"""Check model selection, credential fallback and actual per-agent execution."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage

from smart_cs.agents.supervisor import create_supervisor_graph
from smart_cs.memory.long_term import LongTermMemory
from smart_cs.models import AGENT_MODEL_PREFIXES, build_agent_models, model_credentials_configured, resolve_agent_model_settings


class NamedModel(FakeListChatModel):
    model_name: str


class ModelSettingsTests(unittest.TestCase):
    def test_role_override_and_blank_setting_fall_back_to_common_model(self):
        env = {'MODEL_NAME': 'shared', 'OPENAI_API_KEY': 'shared-test-key',
               'OPENAI_BASE_URL': 'https://gateway.example/v1',
               'INTENT_MODEL_NAME': 'classifier', 'KNOWLEDGE_MODEL_NAME': ' '}
        self.assertEqual(resolve_agent_model_settings('intent_router', env).model_name, 'classifier')
        self.assertEqual(resolve_agent_model_settings('knowledge_rag', env).model_name, 'shared')
        self.assertEqual(resolve_agent_model_settings('ticket_handler', env).api_key, 'shared-test-key')

    def test_explicit_role_credentials_do_not_replace_other_roles(self):
        env = {'MODEL_NAME': 'shared', 'OPENAI_API_KEY': 'shared-test-key', 'OPENAI_BASE_URL': 'https://common.example/v1',
               'COMPLIANCE_OPENAI_API_KEY': 'review-test-key', 'COMPLIANCE_OPENAI_BASE_URL': 'https://review.example/v1'}
        settings = resolve_agent_model_settings('compliance_checker', env)
        self.assertEqual(settings.api_key, 'review-test-key')
        self.assertEqual(settings.base_url, 'https://review.example/v1')
        self.assertEqual(resolve_agent_model_settings('ticket_handler', env).api_key, 'shared-test-key')
        self.assertNotIn('review-test-key', repr(settings))

    def test_a_different_provider_requires_an_explicit_role_key(self):
        env = {'MODEL_NAME': 'shared', 'OPENAI_API_KEY': 'common-test-key', 'OPENAI_BASE_URL': 'https://common.example/v1',
               'KNOWLEDGE_OPENAI_BASE_URL': 'https://other.example/v1'}
        with self.assertRaises(ValueError):
            resolve_agent_model_settings('knowledge_rag', env)

    def test_common_settings_reuse_one_client(self):
        with patch.dict(os.environ, {'MODEL_NAME': 'shared', 'OPENAI_API_KEY': 'test-key'}, clear=True):
            with patch('smart_cs.models.ChatOpenAI', return_value=NamedModel(model_name='shared', responses=['test'])) as create:
                with patch('smart_cs.models.httpx.Client'), patch('smart_cs.models.httpx.AsyncClient'):
                    models = build_agent_models()
                self.assertEqual(create.call_count, 1)
                self.assertTrue(create.call_args.kwargs["disable_streaming"])
                self.assertEqual(len({id(model) for model in models.values()}), 1)

    def test_factory_instantiates_each_configured_role_model(self):
        env = {'MODEL_NAME': 'shared', 'OPENAI_API_KEY': 'test-key'}
        selected = {role: f'{role}-model' for role in AGENT_MODEL_PREFIXES}
        for role, prefix in AGENT_MODEL_PREFIXES.items():
            env[f'{prefix}_MODEL_NAME'] = selected[role]
        with patch.dict(os.environ, env, clear=True):
            with patch('smart_cs.models.ChatOpenAI', side_effect=lambda **kw: NamedModel(model_name=kw['model'], responses=['test'])) as create:
                with patch('smart_cs.models.httpx.Client'), patch('smart_cs.models.httpx.AsyncClient'):
                    models = build_agent_models()
                self.assertEqual({role: model.model_name for role, model in models.items()}, selected)
                self.assertEqual(create.call_count, 4)

    def test_injected_shared_model_and_role_override_skip_provider_clients(self):
        common = NamedModel(model_name='common', responses=['test'])
        router = NamedModel(model_name='router', responses=['test'])
        with patch('smart_cs.models.ChatOpenAI') as create:
            models = build_agent_models(default_model=common, overrides={'intent_router': router})
            create.assert_not_called()
        self.assertIs(models['intent_router'], router)
        self.assertIs(models['ticket_handler'], common)
        with self.assertRaises(ValueError):
            build_agent_models(default_model=common, overrides={'unknown': common})

    def test_configuration_requires_credentials_for_all_roles(self):
        env = {f'{prefix}_OPENAI_API_KEY': 'test-key' for prefix in AGENT_MODEL_PREFIXES.values()}
        env.pop('COMPLIANCE_OPENAI_API_KEY')
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(model_credentials_configured())
            os.environ['COMPLIANCE_OPENAI_API_KEY'] = 'review-key'
            self.assertTrue(model_credentials_configured())

    def test_invalid_numeric_settings_are_rejected(self):
        for key, value in [('INTENT_TEMPERATURE', 'NaN'), ('INTENT_TEMPERATURE', '3'), ('INTENT_TIMEOUT', '-1')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                resolve_agent_model_settings('intent_router', {key: value})


class PerAgentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_correct_models_execute_each_branch_and_usage_resets_per_turn(self):
        router = NamedModel(model_name='router-model', responses=[
            '{"primary_intent":"consultation","confidence":0.95,"suggested_agent":"knowledge_rag"}',
            '{"primary_intent":"transaction","confidence":0.95,"suggested_agent":"ticket_handler"}',
            '{"primary_intent":"compliance","confidence":0.95,"suggested_agent":"compliance_checker"}',
        ])
        knowledge = NamedModel(model_name='answer-model', responses=['退款政策', '0', '7天内可申请退款。[refund_policy.md]'])
        ticket = NamedModel(model_name='ticket-model', responses=['{"action":"create","ticket_type":"refund","summary":"申请退款"}'])
        review = NamedModel(model_name='review-model', responses=[
            '{"passed":true,"risk_level":"low"}', '{"passed":true,"risk_level":"low"}',
            '请通过平台官方渠道核实，不要提供验证码。', '{"passed":true,"risk_level":"low"}',
        ])
        with tempfile.TemporaryDirectory() as folder:
            memory = LongTermMemory(Path(folder) / 'index')
            memory.add_document('购买7天内可申请退款。', 'refund_policy.md')
            graph = create_supervisor_graph(long_term_memory=memory, agent_llms={
                'intent_router': router, 'knowledge_rag': knowledge, 'ticket_handler': ticket, 'compliance_checker': review,
            })
            config = {'configurable': {'thread_id': 'models-test'}}
            first = await graph.ainvoke({'messages': [HumanMessage(content='退款政策是什么？')], 'user_id': 'test',
                                        'session_id': 'models-test', 'sub_results': {}}, config)
            self.assertEqual(first['agent_models'], {'intent_router': 'router-model', 'knowledge_rag': 'answer-model', 'compliance_checker': 'review-model'})
            second = await graph.ainvoke({'messages': [HumanMessage(content='申请退款')], 'sub_results': {}}, config)
            self.assertIn('类型: refund', second['final_response'])
            self.assertEqual(second['agent_models'], {'intent_router': 'router-model', 'ticket_handler': 'ticket-model', 'compliance_checker': 'review-model'})
            third = await graph.ainvoke({'messages': [HumanMessage(content='有人索要验证码')], 'sub_results': {}}, config)
            self.assertIn('不要提供验证码', third['final_response'])
            self.assertEqual(third['agent_models'], {'intent_router': 'router-model', 'compliance_checker': 'review-model'})


if __name__ == '__main__':
    unittest.main()
