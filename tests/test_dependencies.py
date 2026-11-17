from legacy_identity import override_identity
import asyncio
import json
import io
from pathlib import Path
import tempfile
from contextlib import redirect_stdout
import unittest
from unittest.mock import AsyncMock,patch
import httpx
from fastapi.testclient import TestClient
from smart_cs.services import dependencies as deps
from smart_cs.schemas.dependencies import DependencyCheck,DependencyReport
from smart_cs.retrieval.endpoints import validate_local_endpoint
from smart_cs.foundation import ensure_services
import smart_cs.api.main as main

CLIENT=httpx.AsyncClient
DIGEST='d'*64
ENV={'RAG_ENABLED':'true','RAG_EMBEDDING_URL':'http://127.0.0.1:61535','RAG_RERANKER_URL':'http://127.0.0.1:61536','RAG_EMBEDDING_MODEL':'local-bge'}


class ParsingTests(unittest.TestCase):
    def test_wsl_utf16_is_decoded_without_garbled_nuls(self):
        value='  NAME STATE VERSION\r\n* docker-desktop Running 2\r\n'
        self.assertEqual(deps.decode_command_output(value.encode('utf-16-le')),value)

    def test_compose_report_drops_command_labels_environment_and_unrelated_services(self):
        raw=json.dumps([{'Service':'postgres','State':'running','Health':'healthy','Image':'pgvector',
            'Command':'PRIVATE_COMMAND','Labels':'PRIVATE_LABEL','Environment':{'password':'PRIVATE_PASSWORD'},'Publishers':[]},
            {'Service':'unrelated','State':'running'}])
        parsed=deps.parse_compose_ps(raw)
        self.assertEqual(set(parsed),{'postgres'});self.assertNotIn('PRIVATE',json.dumps(parsed))

    def test_optional_unknown_chat_health_never_claims_provider_success(self):
        checks={'postgres':DependencyCheck(status='ready',required=True),'chat':DependencyCheck(status='not_checked',required=False)}
        self.assertTrue(deps.report_ready(checks))
        checks['redis']=DependencyCheck(status='failed',required=True)
        self.assertFalse(deps.report_ready(checks))

    def test_local_url_validation_rejects_credentials_redirect_hosts_paths_and_zero_port(self):
        for url in ['https://example.com','http://127.0.0.1:1234@evil.invalid','http://localhost:1234/path','http://localhost:1234?key=secret','http://localhost:0']:
            with self.subTest(url=url),self.assertRaises(ValueError):validate_local_endpoint(url)
        self.assertEqual(validate_local_endpoint('http://localhost:61535/'),'http://localhost:61535')

    def test_start_waits_for_only_core_services_without_deletion(self):
        with patch('smart_cs.foundation.subprocess.run') as run:
            run.return_value.returncode=0;ensure_services()
            args=run.call_args.args[0]
            self.assertEqual(args[-2:],['postgres','redis']);self.assertIn('--wait',args)
            self.assertNotIn('down',args);self.assertNotIn('--volumes',args)

    def test_cli_failure_replaces_latest_success_without_leaking_errors(self):
        import smart_cs.infrastructure as cli
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'.local/infrastructure';output.mkdir(parents=True)
            (output/'latest.json').write_text('{"ready":true}',encoding='utf-8')
            captured=io.StringIO()
            with patch.object(cli,'PROJECT_ROOT',root),patch.object(cli,'load_dotenv'),patch('sys.argv',['infrastructure','start']),patch('smart_cs.foundation.ensure_services',side_effect=RuntimeError('PRIVATE_PASSWORD')),redirect_stdout(captured):
                self.assertEqual(cli.main(),1)
            report=DependencyReport.model_validate_json((output/'latest.json').read_text(encoding='utf-8'))
            self.assertFalse(report.ready);self.assertNotIn('PRIVATE_PASSWORD',captured.getvalue())


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def client_patch(self,handler):
        transport=httpx.MockTransport(handler)
        return patch.object(deps.httpx,'AsyncClient',side_effect=lambda **kwargs:CLIENT(transport=transport,**kwargs))

    async def test_timeout_and_error_reports_never_contain_raw_provider_payloads(self):
        async def broken():raise RuntimeError('postgresql://user:PRIVATE_PASSWORD@server')
        result=await deps.checked(broken)
        self.assertEqual(result.status,'failed');self.assertNotIn('PRIVATE_PASSWORD',result.model_dump_json())
        async def slow():await asyncio.sleep(1)
        result=await deps.checked(slow,timeout=.001)
        self.assertEqual(result.code,'DEPENDENCY_TIMEOUT')

    async def test_embedding_dimensions_are_measured_and_invalid_vectors_fail(self):
        dimension=1024
        def handler(request):
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':'local-bge:latest','digest':DIGEST}]})
            return httpx.Response(200,json={'embeddings':[[1.]*dimension,[1.]*dimension]})
        with self.client_patch(handler):
            details=await deps.DependencyChecker(ENV).embedding(True)
            self.assertEqual(details['dimensions'],1024);self.assertEqual(details['inference'],'verified')
            dimension=768
            result=await deps.checked(lambda:deps.DependencyChecker(ENV).embedding(True))
            self.assertEqual(result.code,'EMBEDDING_RESPONSE_INVALID')

    async def test_chat_compatible_json_is_not_embedding_support(self):
        with self.client_patch(lambda request:httpx.Response(200,json={'choices':[{'message':{'content':'hello'}}]})):
            result=await deps.checked(lambda:deps.DependencyChecker(ENV).embedding(False))
            self.assertEqual(result.code,'EMBEDDING_MODEL_NOT_REGISTERED')

    async def test_reranker_health_does_not_accept_an_arbitrary_http_service(self):
        with self.client_patch(lambda request:httpx.Response(200,json={'ready':True,'service':'other','model':'bge-reranker-v2-m3'})):
            result=await deps.checked(lambda:deps.DependencyChecker(ENV).reranker(False))
            self.assertEqual(result.code,'RERANKER_IDENTITY_NOT_READY')

    async def test_reranker_probes_scores_and_tokenizer_instead_of_only_health(self):
        scores=[.9,.01]
        def handler(request):
            if request.url.path=='/health':return httpx.Response(200,json={'ready':True,'service':'smart-cs-local-reranker','model':'bge-reranker-v2-m3','device':'cpu'})
            if request.url.path=='/rerank':return httpx.Response(200,json={'scores':scores})
            return httpx.Response(200,json={'pieces':[[{'text':'test','tokens':3}]]})
        with self.client_patch(handler):
            result=await deps.DependencyChecker(ENV).reranker(True)
            self.assertEqual(result['tokenizer'],'verified')
            scores=[.01,.9]
            failed=await deps.checked(lambda:deps.DependencyChecker(ENV).reranker(True))
            self.assertEqual(failed.code,'RERANKER_PROBE_FAILED')

    async def test_index_model_digest_mismatch_blocks_readiness(self):
        class Checker(deps.DependencyChecker):
            async def postgres(self):return {'active_index':{'embedding':'different@'+DIGEST}}
            async def redis(self,deep=False):return {'ping':True}
            async def embedding(self,deep=False):return {'model':'local-bge','digest':DIGEST}
            async def reranker(self,deep=False):return {'service':'ready'}
        report=await Checker(ENV).run()
        self.assertFalse(report.ready)
        self.assertEqual(report.checks['index_compatibility'].code,'EMBEDDING_INDEX_IDENTITY_MISMATCH')

    async def test_redis_probe_only_cleans_up_its_own_temporary_key(self):
        from redis.asyncio import Redis
        class Fake:
            ping=AsyncMock(return_value=True);set=AsyncMock(return_value=True);get=AsyncMock(return_value='dependency-probe')
            ttl=AsyncMock(return_value=9);delete=AsyncMock();aclose=AsyncMock()
        client=Fake()
        with patch.object(Redis,'from_url',return_value=client):
            result=await deps.DependencyChecker({'REDIS_URL':'redis://localhost:6379/0'}).redis(True)
            self.assertEqual(result['read_write_ttl'],'verified')
            key=client.set.call_args.args[0]
            self.assertTrue(key.startswith('smartcs:dependency-check:'));client.delete.assert_awaited_once_with(key)
            client.aclose.assert_awaited_once()


class HealthApiTests(unittest.TestCase):
    def test_alive_is_separate_from_failed_dependencies(self):
        report=DependencyReport(checked_at='2026-11-14T00:00:00+00:00',profile='connectivity',ready=False,
            checks={'redis':DependencyCheck(status='failed',required=True,code='DEPENDENCY_UNAVAILABLE')})
        with patch.object(main,'dependency_status',AsyncMock(return_value=report)):
            context=override_identity(main.app);context.__enter__();self.addCleanup(context.__exit__,None,None,None)
            client=TestClient(main.app)
            self.assertEqual(client.get('/health/live').status_code,200)
            self.assertEqual(client.get('/health/ready').status_code,503)
            self.assertFalse(client.get('/health/ready').json()['ready'])
            self.assertEqual(client.get('/health').json()['upstream_model_health'],'not_probed')

if __name__=='__main__':unittest.main()
