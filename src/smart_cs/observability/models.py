import asyncio
import functools
from time import perf_counter
from uuid import uuid4
from smart_cs.observability.context import run_context,operation_context
from smart_cs.schemas.errors import classify_failure
from smart_cs.tracing.otel_config import get_tracer
from smart_cs.performance.policy import ServingPolicy
from smart_cs.performance.runtime import slot,reserve,remaining,retryable,event


def extract_usage(message):
    usage=getattr(message,'usage_metadata',None)
    source='langchain_usage_metadata'
    if not isinstance(usage,dict) or not usage:
        metadata=getattr(message,'response_metadata',None) or {}
        if not isinstance(metadata,dict):metadata={}
        usage=metadata.get('token_usage') or metadata.get('usage') or {}
        source='provider_response_metadata'
    if not isinstance(usage,dict):usage={}
    def count(*keys):
        for key in keys:
            value=usage.get(key)
            if isinstance(value,int) and not isinstance(value,bool) and value>=0:return value
        return None
    result={'input_tokens':count('input_tokens','prompt_tokens'),'output_tokens':count('output_tokens','completion_tokens'),'total_tokens':count('total_tokens')}
    available=sum(v is not None for v in result.values())
    return {**result,'status':'available' if available==3 else 'partial' if available else 'unknown','source':source if available else 'missing'}


def _role(operation):
    if operation.startswith('intent'):return 'intent_router'
    if operation.startswith(('knowledge','rag')):return 'knowledge_rag'
    if operation.startswith('ticket'):return 'ticket_handler'
    if operation.startswith('compliance'):return 'compliance_checker'
    return 'unknown'


async def _attempt(invoke,model,kind='chat',role=None,attempt=1,queue_ms=0):
    observation=run_context.get()
    if observation is None:return await invoke()
    operation=operation_context.get();started=perf_counter();message=None
    record={'id':uuid4().hex,'kind':kind,'model':str(model)[:160],'agent_role':role or _role(operation),
            'operation':operation,'status':'succeeded','error_code':None,'span_id':None,
            'run_id':observation.run_id,'request_id':observation.request_id,'trace_id':observation.trace_id,
            'attempt':attempt,'queue_ms':queue_ms}
    if observation.sink is not None:await observation.sink('start',record)
    tracer=get_tracer()
    from contextlib import nullcontext
    manager=tracer.start_as_current_span('model.'+kind,record_exception=False,set_status_on_exception=False) if tracer else nullcontext(None)
    with manager as span:
        if span:
            sc=span.get_span_context();record['span_id']=f'{sc.span_id:016x}' if sc.is_valid else None
            span.set_attribute('app.run_id',observation.run_id);span.set_attribute('app.request_id',observation.request_id)
            span.set_attribute('app.trace_id',observation.trace_id);span.set_attribute('model.name',record['model']);span.set_attribute('model.kind',kind)
        try:
            message=await invoke();return message
        except BaseException as error:
            record['status']='cancelled' if isinstance(error,asyncio.CancelledError) else 'failed'
            record['error_code']=classify_failure(error)[0]
            if span:span.set_attribute('error.code',record['error_code'])
            raise
        finally:
            record['duration_ms']=round((perf_counter()-started)*1000,3)
            record['usage']=extract_usage(message)
            metadata=getattr(message,'response_metadata',{}) or {}
            if isinstance(metadata,dict) and isinstance(metadata.get('model_name'),str):record['response_model']=metadata['model_name'][:160]
            observation.calls.append(record)
            if observation.sink is not None:await observation.sink('finish',record)
            if span:
                span.set_attribute('model.duration_ms',record['duration_ms']);span.set_attribute('model.usage_status',record['usage']['status'])
                for key in ['input_tokens','output_tokens','total_tokens']:
                    if record['usage'][key] is not None:span.set_attribute('model.'+key,record['usage'][key])


async def observe(invoke,model,kind='chat',role=None):
    policy=ServingPolicy.deployed();observation=run_context.get();role=role or _role(operation_context.get())
    if not policy.enabled:return await _attempt(invoke,model,kind,role)
    for attempt in range(1,(policy.max_retries if kind=='chat' else 0)+2):
        try:
            async with slot(observation,policy,kind,role) as queue_ms:
                reserve(observation,policy,kind)
                async def limited():
                    # Preserve the provider's TimeoutError contract. The existing
                    # error classifier maps it to MODEL_TIMEOUT without a retry.
                    async with asyncio.timeout(min(policy.call_seconds,remaining(observation,policy))):return await invoke()
                return await _attempt(limited,model,kind,role,attempt,queue_ms)
        except Exception as error:
            if attempt>policy.max_retries or kind!='chat' or not retryable(error):raise
            wait=min(.5*attempt,remaining(observation,policy))
            event(observation,'retry',role=role,attempt=attempt+1,backoff_ms=wait*1000)
            await asyncio.sleep(wait)


async def invoke_model(model,*args,**kwargs):
    from langchain_openai import ChatOpenAI
    selected=model
    policy=ServingPolicy.deployed()
    if policy.enabled and isinstance(model,ChatOpenAI):selected=model.bind(max_tokens=policy.max_output_tokens)
    return await observe(lambda:selected.ainvoke(*args,**kwargs),getattr(model,'model_name',type(model).__name__))


def local_model(kind):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(self,*args,**kwargs):
            name=getattr(self,'model','bge-m3') if kind=='embedding' else 'bge-reranker-v2-m3'
            return await observe(lambda:function(self,*args,**kwargs),name,kind,role='knowledge_rag')
        return wrapped
    return decorate
