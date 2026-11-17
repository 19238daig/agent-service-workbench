from legacy_identity import install_offline_identity
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from smart_cs.observability.models import extract_usage,observe
from smart_cs.observability.context import RunObservation,run_context,usage_summary
from smart_cs.repositories.runs import MemoryRunRepository,DatabaseRunRepository
from smart_cs.services.chat import ChatService
from smart_cs.schemas.chat import ChatRequest
from smart_cs.schemas.identity import IdentityContext
from smart_cs.db.database import Database
from smart_cs.tracing.otel_config import AgentMetrics
from smart_cs.memory.short_term import ShortTermMemory
from smart_cs.memory.long_term import LongTermMemory
from smart_cs.agents.supervisor import create_supervisor_graph
from test_workflow_api import client_for,classify,PASS,events
import smart_cs.api.main as main


class UsageTests(unittest.TestCase):
    def test_usage_preserves_unknown_partial_reported_and_real_zero(self):
        unknown=extract_usage(AIMessage(content='private'))
        self.assertEqual(unknown,{'input_tokens':None,'output_tokens':None,'total_tokens':None,'status':'unknown','source':'missing'})
        partial=extract_usage(SimpleNamespace(response_metadata={'token_usage':{'prompt_tokens':7}}))
        self.assertEqual(partial['status'],'partial');self.assertIsNone(partial['total_tokens'])
        usage=extract_usage(AIMessage(content='private',usage_metadata={'input_tokens':7,'output_tokens':0,'total_tokens':7}))
        self.assertEqual(usage['output_tokens'],0);self.assertEqual(usage['status'],'available')
        invalid=extract_usage(SimpleNamespace(response_metadata={'usage':{'prompt_tokens':True,'completion_tokens':-1}}))
        self.assertEqual(invalid['status'],'unknown')
        summary=usage_summary([{'kind':'chat','usage':unknown},{'kind':'chat','usage':usage}])
        self.assertIsNone(summary['total_tokens']);self.assertEqual(summary['status'],'partial')


class RecordedApiTests(unittest.TestCase):
    def setUp(self):
        install_offline_identity(self, main.app)
        p=patch.object(main,'configured_database',return_value=None);p.start();self.addCleanup(p.stop)

    def test_sse_headers_frames_and_query_share_ids_and_usage_is_not_zero_filled(self):
        with client_for([classify(primary='unknown',confidence=.1),PASS]) as client:
            response=client.post('/api/chat/stream',json={'message':'help','user_id':'owner'})
            frames=events(response);final=frames[-1]
            self.assertEqual(final['type'],'complete')
            for header,field in [('x-request-id','request_id'),('x-trace-id','trace_id'),('x-run-id','run_id')]:
                self.assertEqual(response.headers[header],final[field])
                self.assertTrue(all(f[field]==final[field] for f in frames))
            record=client.get('/api/runs/'+final['run_id'],params={'user_id':'owner'}).json()
            self.assertEqual(record['status'],'succeeded');self.assertFalse(record['identity']['authenticated'])
            self.assertEqual(record['usage']['status'],'unknown');self.assertIsNone(record['usage']['total_tokens'])
            self.assertEqual(len(record['model_calls']),2)
            self.assertEqual({c['agent_role'] for c in record['model_calls']},{'intent_router','compliance_checker'})
            self.assertEqual(client.get('/api/runs/'+final['run_id'],params={'user_id':'other'}).status_code,404)
            self.assertNotIn('help',json.dumps(record))

    def test_json_response_and_real_usage_metadata_are_recorded_without_text(self):
        class Metered(FakeListChatModel):
            async def ainvoke(self,*args,**kwargs):
                message=await super().ainvoke(*args,**kwargs)
                message.usage_metadata={'input_tokens':11,'output_tokens':3,'total_tokens':14}
                return message
        with client_for([],Metered(responses=[classify(primary='unknown',confidence=.1),PASS])) as client:
            reply=client.post('/api/chat',json={'message':'PRIVATE_INPUT','user_id':'metered'}).json()
            record=client.get('/api/runs/'+reply['run_id'],params={'user_id':'metered'}).json()
            self.assertEqual(record['usage']['total_tokens'],28)
            self.assertEqual(record['usage']['status'],'available')
            self.assertNotIn('PRIVATE_INPUT',json.dumps(record))
            self.assertTrue(all(c['duration_ms']>=0 for c in record['model_calls']))

    def test_failed_stream_has_queryable_sanitized_failure_and_no_final_reply(self):
        class Broken(FakeListChatModel):
            async def ainvoke(self,*args,**kwargs):raise TimeoutError('DO_NOT_STORE_API_KEY_OR_PROVIDER_BODY')
        with client_for([],Broken(responses=['unused'])) as client:
            response=client.post('/api/chat/stream',json={'message':'secret','user_id':'broken'})
            frames=events(response);last=frames[-1]
            self.assertEqual(last['code'],'MODEL_TIMEOUT')
            record=client.get('/api/runs/'+last['run_id'],params={'user_id':'broken'}).json()
            self.assertEqual(record['status'],'failed')
            self.assertEqual(record['model_calls'][0]['error_code'],'MODEL_TIMEOUT')
            self.assertNotIn('DO_NOT_STORE',json.dumps(record))
            self.assertFalse(any(e['type']=='complete' for e in frames))

    def test_validation_and_configuration_errors_have_stable_codes_and_no_input_echo(self):
        with patch.object(main,'graph',None),patch.object(main,'initialize_graph'),patch.object(main,'init_tracer'):
            with TestClient(main.app) as client:
                bad=client.post('/api/chat',json={'message':'PRIVATE_VALUE'*1000})
                self.assertEqual(bad.status_code,422);self.assertEqual(bad.json()['error']['code'],'VALIDATION_ERROR')
                self.assertNotIn('PRIVATE_VALUE',bad.text)
                self.assertEqual(bad.json()['error']['request_id'],bad.headers['x-request-id'])
                unavailable=client.post('/api/chat',json={'message':'help'})
                self.assertEqual(unavailable.json()['error']['code'],'MODEL_NOT_CONFIGURED')


class RepositoryAndConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_runs_keep_calls_and_trace_ids_separate(self):
        from smart_cs.observability.context import RequestCorrelation,request_context
        async def one():
            correlation=RequestCorrelation();request_token=request_context.set(correlation)
            observation=RunObservation(correlation.request_id,correlation.request_id,correlation.trace_id)
            token=run_context.set(observation)
            try:
                async def invoke():
                    await asyncio.sleep(.01)
                    return AIMessage(content='not stored',usage_metadata={'input_tokens':2,'output_tokens':1,'total_tokens':3})
                await observe(invoke,'model')
                return observation
            finally:run_context.reset(token);request_context.reset(request_token)
        a,b=await asyncio.gather(one(),one())
        self.assertNotEqual(a.trace_id,b.trace_id)
        self.assertEqual(a.calls[0]['run_id'],a.run_id);self.assertEqual(b.calls[0]['run_id'],b.run_id)
        self.assertEqual(len(a.calls),1);self.assertEqual(len(b.calls),1)

    async def test_database_records_survive_new_engine_and_filter_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            url='sqlite+aiosqlite:///'+str(Path(directory)/'runs.db').replace('\\','/')
            db=Database(url);await db.initialize(create_schema=True)
            record={'run_id':'a'*32,'request_id':'b'*32,'trace_id':'c'*32,'session_id':'s','identity':IdentityContext(subject_id='owner').model_dump(),
                    'status':'running','recording_backend':'database','nodes':[],'duration_ms':None,'error_code':None,'usage':usage_summary([])}
            repo=DatabaseRunRepository(db);await repo.start(record)
            record.update(status='succeeded',duration_ms=1)
            await repo.finish(record,[]);await db.close()
            db=Database(url)
            try:
                self.assertEqual((await DatabaseRunRepository(db).get('a'*32,'owner'))['status'],'succeeded')
                self.assertIsNone(await DatabaseRunRepository(db).get('a'*32,'other'))
            finally:await db.close()

    async def test_cancellation_records_model_failure_and_does_not_invent_tokens(self):
        observation=RunObservation('a'*32,'b'*32,'c'*32);token=run_context.set(observation)
        async def cancelled():raise asyncio.CancelledError()
        try:
            with self.assertRaises(asyncio.CancelledError):await observe(cancelled,'model')
            self.assertEqual(observation.calls[0]['status'],'cancelled')
            self.assertEqual(observation.calls[0]['usage']['status'],'unknown')
        finally:run_context.reset(token)

    async def test_start_record_failure_does_not_execute_workflow(self):
        class Broken(MemoryRunRepository):
            async def start(self,record):raise RuntimeError('private db URL')
        class Workflow:
            called=False
            async def astream_events(self,*args,**kwargs):
                self.called=True
                yield {}
        workflow=Workflow();service=ChatService(workflow,None,Broken(),AgentMetrics());request=ChatRequest(message='test')
        output=[e async for e in service.events(request,service.prepare(request))]
        self.assertEqual(output[0]['code'],'RUN_RECORDING_UNAVAILABLE');self.assertFalse(workflow.called)

    async def test_closing_stream_records_cancellation_and_restores_context(self):
        repo=MemoryRunRepository();service=ChatService(None,None,repo,AgentMetrics());request=ChatRequest(message='test')
        prepared=service.prepare(request);stream=service.events(request,prepared)
        self.assertEqual((await anext(stream))['type'],'session')
        await stream.aclose()
        record=await repo.get(prepared.run_id,'anonymous')
        self.assertEqual(record['status'],'cancelled')
        self.assertIsNone(run_context.get())

if __name__=='__main__':unittest.main()
