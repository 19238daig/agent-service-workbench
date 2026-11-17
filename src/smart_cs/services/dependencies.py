"""Bounded real dependency checks. Never serialize credentials or raw errors."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from time import perf_counter
from uuid import uuid4
import httpx
import numpy as np
from sqlalchemy import text,select
from smart_cs.schemas.dependencies import DependencyReport,DependencyCheck
from smart_cs.db.database import Database
from smart_cs.db.models import RetrievalIndex
from smart_cs.retrieval.clients import LocalModels
from smart_cs.retrieval.endpoints import validate_local_endpoint
from smart_cs.settings import PROJECT_ROOT


class DependencyFailure(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)


async def checked(function,required=True,timeout=6):
    started=perf_counter()
    try:
        details=await asyncio.wait_for(function(),timeout=timeout)
        return DependencyCheck(status='ready',required=required,details=details,duration_ms=round((perf_counter()-started)*1000,2))
    except Exception as error:
        code=error.code if isinstance(error,DependencyFailure) else 'DEPENDENCY_TIMEOUT' if isinstance(error,TimeoutError) else 'DEPENDENCY_UNAVAILABLE'
        return DependencyCheck(status='failed',required=required,code=code,duration_ms=round((perf_counter()-started)*1000,2),details={'error_type':type(error).__name__})


def report_ready(checks):return all(check.status in {'ready','not_applicable'} for check in checks.values() if check.required)


def decode_command_output(raw):
    if isinstance(raw,str):return raw
    return raw.decode('utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-16-le' if b'\x00' in raw[:120] else 'utf-8',errors='replace').strip('\ufeff\x00')


def command(args,timeout=10):
    result=subprocess.run(args,cwd=PROJECT_ROOT,capture_output=True,timeout=timeout)
    if result.returncode:raise DependencyFailure('HOST_COMMAND_FAILED')
    return decode_command_output(result.stdout)


def parse_compose_ps(output):
    output=output.strip()
    rows=json.loads(output) if output.startswith('[') else [json.loads(line) for line in output.splitlines() if line.strip()]
    return {r['Service']:{'state':r.get('State'),'health':r.get('Health'),'image':r.get('Image'),
        'ports':[{'address':p.get('URL'),'published':p.get('PublishedPort'),'target':p.get('TargetPort')} for p in r.get('Publishers') or []]} for r in rows if r.get('Service') in {'postgres','redis'}}


def host_checks():
    checks={}
    try:
        if shutil.which('docker') is None:raise DependencyFailure('DOCKER_NOT_INSTALLED')
        version=json.loads(command(['docker','version','--format','{{json .}}']))
        server=version.get('Server') or {}
        if server.get('Os')!='linux':raise DependencyFailure('DOCKER_LINUX_ENGINE_REQUIRED')
        checks['docker']=DependencyCheck(status='ready',required=True,details={'client_version':version['Client']['Version'],
            'server_version':server['Version'],'engine_os':server['Os'],'platform':server.get('Platform',{}).get('Name')})
        services=parse_compose_ps(command(['docker','compose','-p','smart-cs-foundation','-f',str(PROJECT_ROOT/'docker-compose.yml'),'ps','--format','json']))
        good=all(services.get(name,{}).get('state')=='running' and services[name].get('health')=='healthy' for name in ['postgres','redis'])
        checks['compose']=DependencyCheck(status='ready' if good else 'failed',required=True,code=None if good else 'COMPOSE_SERVICES_UNHEALTHY',details={'services':services})
    except Exception as error:
        checks['docker']=DependencyCheck(status='failed',required=True,code=getattr(error,'code','DOCKER_UNAVAILABLE'),details={'error_type':type(error).__name__})
    if platform.system()=='Windows':
        try:
            if shutil.which('wsl') is None:raise DependencyFailure('WSL_NOT_INSTALLED')
            output=command(['wsl','--list','--verbose'])
            version2=any(re.search(r'\s2\s*$',line) for line in output.splitlines())
            if not version2:raise DependencyFailure('WSL2_DISTRIBUTION_NOT_FOUND')
            checks['wsl2']=DependencyCheck(status='ready',required=True,details={'version_2_distribution_present':True,
                'docker_desktop_distribution_present':'docker-desktop' in output})
        except Exception as error:checks['wsl2']=DependencyCheck(status='failed',required=True,code=getattr(error,'code','WSL_CHECK_FAILED'),details={'error_type':type(error).__name__})
    else:checks['wsl2']=DependencyCheck(status='not_applicable',required=False,details={'reason':'WSL2 is a Windows host requirement'})
    return checks


class DependencyChecker:
    def __init__(self,environ=None):self.env=dict(os.environ if environ is None else environ)

    @property
    def rag_enabled(self):return self.env.get('RAG_ENABLED','false').lower()=='true'

    def model_endpoints(self):
        try:
            return (validate_local_endpoint(self.env.get('RAG_EMBEDDING_URL','http://127.0.0.1:61535')),
                    validate_local_endpoint(self.env.get('RAG_RERANKER_URL','http://127.0.0.1:61536')))
        except ValueError:raise DependencyFailure('LOCAL_MODEL_ENDPOINT_INVALID') from None

    async def postgres(self):
        url=self.env.get('DATABASE_URL','').strip()
        if not url.startswith('postgresql+asyncpg://'):raise DependencyFailure('POSTGRESQL_NOT_CONFIGURED')
        database=Database(url)
        try:
            async with database.sessions() as session:
                await session.execute(text('SELECT 1'))
                version=await session.scalar(text("SELECT current_setting('server_version')"))
                extension=await session.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
                if not extension:raise DependencyFailure('PGVECTOR_EXTENSION_MISSING')
                distance=await session.scalar(text("SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector"))
                if float(distance)!=0:raise DependencyFailure('PGVECTOR_OPERATOR_FAILED')
                details={'backend':'postgresql','server_version':version,'pgvector_version':extension,'select_1':True,'vector_operator':True}
                if self.rag_enabled:
                    active=await session.scalar(select(RetrievalIndex).where(RetrievalIndex.active.is_(True)))
                    if active is None:raise DependencyFailure('ACTIVE_VECTOR_INDEX_MISSING')
                    if active.configuration.get('protocol')!='dense-exact-v1':raise DependencyFailure('INDEX_UPGRADE_REQUIRED')
                    dimensions=await session.scalar(text('SELECT vector_dims(embedding) FROM knowledge_embeddings WHERE index_id=:id LIMIT 1'),{'id':active.id})
                    if dimensions!=1024 or active.model_identity.get('dimensions')!=1024:raise DependencyFailure('VECTOR_INDEX_DIMENSION_MISMATCH')
                    details['active_index']={'id':active.id,'dimensions':dimensions,'embedding':active.model_identity['embedding'],'corpus':active.configuration['corpus']}
                return details
        finally:await database.close()

    async def redis(self,deep=False):
        from redis.asyncio import Redis
        url=self.env.get('REDIS_URL','').strip()
        if not url.startswith(('redis://','rediss://')):raise DependencyFailure('REDIS_NOT_CONFIGURED')
        client=Redis.from_url(url,socket_connect_timeout=2,socket_timeout=2,decode_responses=True)
        key='smartcs:dependency-check:'+uuid4().hex;created=False
        try:
            if not await client.ping():raise DependencyFailure('REDIS_PING_FAILED')
            details={'ping':True,'read_write_ttl':'not_probed'}
            if deep:
                created=bool(await client.set(key,'dependency-probe',ex=10,nx=True))
                if not created or await client.get(key)!='dependency-probe' or not 0<await client.ttl(key)<=10:raise DependencyFailure('REDIS_READ_WRITE_TTL_FAILED')
                details['read_write_ttl']='verified'
            return details
        finally:
            if created:
                try:await client.delete(key)
                except Exception:pass
            await client.aclose()

    async def embedding(self,deep=False):
        base,_=self.model_endpoints();model=self.env.get('RAG_EMBEDDING_MODEL','smart-cs-bge-m3')
        async with httpx.AsyncClient(trust_env=False,timeout=60 if deep else 3) as client:
            response=await client.get(base+'/api/tags');response.raise_for_status()
            match=next((m for m in response.json().get('models',[]) if m.get('name') in {model,model+':latest'}),None)
            if not match:raise DependencyFailure('EMBEDDING_MODEL_NOT_REGISTERED')
            digest=match.get('digest','')
            if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):raise DependencyFailure('EMBEDDING_IDENTITY_INVALID')
            details={'engine':'ollama','model':model,'digest':digest,'inference':'not_probed','dimensions':None}
            if deep:
                result=await client.post(base+'/api/embed',json={'model':model,'input':['退款申请的处理规则','收到商品后如何申请退货'],'truncate':False,'keep_alive':'30m'})
                result.raise_for_status()
                try:vectors=LocalModels.vectors(result.json()['embeddings'],2)
                except (ValueError,KeyError,TypeError):raise DependencyFailure('EMBEDDING_RESPONSE_INVALID') from None
                details.update(inference='verified',dimensions=len(vectors[0]),vector_count=len(vectors),finite_nonzero_vectors=True,
                    protocol='POST /api/embed; batch input; truncate=false',chat_capability='not_tested_not_required')
            return details

    async def reranker(self,deep=False):
        _,base=self.model_endpoints()
        async with httpx.AsyncClient(trust_env=False,timeout=60 if deep else 3) as client:
            response=await client.get(base+'/health');response.raise_for_status();health=response.json()
            if health.get('service')!='smart-cs-local-reranker' or health.get('ready') is not True or health.get('model')!='bge-reranker-v2-m3':raise DependencyFailure('RERANKER_IDENTITY_NOT_READY')
            details={'service':health['service'],'model':health['model'],'device':health.get('device'),'torch':health.get('torch'),'inference':'not_probed','tokenizer':'not_probed'}
            if deep:
                result=await client.post(base+'/rerank',json={'query':'如何申请退款？','passages':['退款申请请提供订单号和退款原因。','地球围绕太阳公转。']})
                result.raise_for_status();scores=result.json().get('scores')
                if not isinstance(scores,list) or len(scores)!=2 or not all(isinstance(v,(int,float)) and not isinstance(v,bool) and np.isfinite(v) for v in scores) or not scores[0]>scores[1]:raise DependencyFailure('RERANKER_PROBE_FAILED')
                tokenized=await client.post(base+'/tokenize',json={'texts':['退款申请需要订单号。'],'max_tokens':32,'overlap':0})
                tokenized.raise_for_status();pieces=tokenized.json().get('pieces')
                if not isinstance(pieces,list) or len(pieces)!=1 or not pieces[0] or not all(0<p.get('tokens',0)<=32 for p in pieces[0]):raise DependencyFailure('TOKENIZER_PROBE_FAILED')
                details.update(inference='verified',tokenizer='verified',related_score=scores[0],unrelated_score=scores[1],
                    protocol='POST /rerank; POST /tokenize',score_interpretation='uncalibrated; smoke ordering only')
            return details

    async def resources(self):
        source=Path(self.env.get('RAG_EMBEDDING_GGUF',''))
        reranker=Path(self.env.get('RAG_RERANKER_PATH',''))
        worker=Path(self.env.get('RAG_WORKER_PYTHON',''));ollama=Path(self.env.get('RAG_OLLAMA_EXE',''))
        if not source.is_file():raise DependencyFailure('EMBEDDING_GGUF_MISSING')
        with source.open('rb') as stream:
            if stream.read(4)!=b'GGUF':raise DependencyFailure('EMBEDDING_GGUF_HEADER_INVALID')
        required=['config.json','tokenizer.json','model.safetensors']
        if not all((reranker/name).is_file() for name in required):raise DependencyFailure('RERANKER_FILES_INCOMPLETE')
        if not worker.is_file() or not ollama.is_file():raise DependencyFailure('LOCAL_MODEL_EXECUTABLE_MISSING')
        config=json.loads((reranker/'config.json').read_text(encoding='utf-8'))
        if 'XLMRobertaForSequenceClassification' not in config.get('architectures',[]):raise DependencyFailure('RERANKER_ARCHITECTURE_MISMATCH')
        return {'gguf_file':source.name,'gguf_bytes':source.stat().st_size,'gguf_header':True,'reranker_files':required,
            'reranker_architecture':config['architectures'],'worker_python_exists':True,'ollama_executable_exists':True,'weights_downloaded_by_check':False}

    async def run(self,deep=False,include_host=False):
        checks={}
        jobs={'postgres':lambda:self.postgres(),'redis':lambda:self.redis(deep)}
        if self.rag_enabled:jobs.update(embedding=lambda:self.embedding(deep),reranker=lambda:self.reranker(deep))
        values=await asyncio.gather(*(checked(fn,timeout=65 if deep and name in {'embedding','reranker'} else 6) for name,fn in jobs.items()))
        checks.update(zip(jobs,values))
        if self.rag_enabled:
            pg=checks['postgres'];embedding=checks['embedding']
            expected=pg.details.get('active_index',{}).get('embedding')
            actual=embedding.details.get('model','')+'@'+embedding.details.get('digest','')
            good=pg.status=='ready' and embedding.status=='ready' and expected==actual
            checks['index_compatibility']=DependencyCheck(status='ready' if good else 'failed',required=True,code=None if good else 'EMBEDDING_INDEX_IDENTITY_MISMATCH',details={'digest_matches_active_index':good})
            if include_host:checks['local_resources']=await checked(self.resources)
        else:
            for name in ['embedding','reranker','index_compatibility']:checks[name]=DependencyCheck(status='not_checked',required=False,details={'reason':'RAG_ENABLED=false'})
        if include_host:checks.update(await asyncio.to_thread(host_checks))
        try:
            from smart_cs.models import resolve_agent_model_settings
            settings=[(role,resolve_agent_model_settings(role,self.env)) for role in ['intent_router','knowledge_rag','ticket_handler','compliance_checker']]
            checks['chat_providers']=DependencyCheck(status='not_checked',required=False,details={'health':'not_probed',
                'roles':[{'role':role,'model':s.model_name,'credentials_configured':bool(s.api_key) and s.api_key!='sk-your-openai-api-key'} for role,s in settings],
                'paid_calls_performed':False,'embedding_or_reranking_support_inferred':False})
        except Exception:checks['chat_providers']=DependencyCheck(status='not_checked',required=False,code='CHAT_CONFIGURATION_INVALID',details={'health':'not_probed'})
        return DependencyReport(checked_at=datetime.now(timezone.utc).isoformat(),profile='deep' if deep else 'connectivity',ready=report_ready(checks),host_checks_included=include_host,checks=checks)


_cache=None


async def readiness():
    # No deep inference or host commands on an HTTP readiness request.
    global _cache
    now=perf_counter()
    if _cache and now-_cache[0]<10:return _cache[1]
    report=await DependencyChecker().run()
    _cache=(perf_counter(),report)
    return report
