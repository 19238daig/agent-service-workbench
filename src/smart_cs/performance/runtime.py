"""Per-process concurrency and observed per-run budgets, including queue/backoff."""
import asyncio
from contextlib import asynccontextmanager
from time import perf_counter
from weakref import WeakKeyDictionary
from smart_cs.performance.policy import ServingPolicy
from smart_cs.schemas.errors import ApplicationError

_loops=WeakKeyDictionary()


def event(observation,kind,**fields):
    if observation is not None:
        observation.performance.append({'kind':kind,**fields})


def remaining(observation,policy):
    elapsed=perf_counter()-observation.started if observation is not None else 0
    value=policy.round_seconds-elapsed
    if value<=0:raise ApplicationError('ROUND_BUDGET_EXCEEDED','本轮已达到运行预算，请核实已有结果。',504)
    return value


def reserve(observation,policy,kind):
    remaining(observation,policy)
    if observation is None or kind!='chat':return
    if observation.chat_attempts>=policy.max_chat_attempts:
        raise ApplicationError('MODEL_CALL_BUDGET_EXCEEDED','本轮已达到模型调用预算。',429)
    known=sum(c['usage']['total_tokens'] or 0 for c in observation.calls if c['kind']=='chat')
    if known>=policy.reported_token_budget:
        raise ApplicationError('MODEL_TOKEN_BUDGET_EXCEEDED','本轮已达到已报告Token预算。',429)
    observation.chat_attempts+=1


@asynccontextmanager
async def slot(observation,policy,kind,role):
    loop=asyncio.get_running_loop()
    slots=_loops.setdefault(loop,{})
    limits=[('global',policy.global_concurrency),((kind,role),policy.role_concurrency if kind=='chat' else policy.local_concurrency)]
    acquired=[];started=perf_counter()
    try:
        for key,limit in limits:
            semaphore=slots.setdefault((key,limit),asyncio.Semaphore(limit))
            timeout=min(policy.queue_seconds-(perf_counter()-started),remaining(observation,policy))
            if timeout<=0:raise ApplicationError('MODEL_QUEUE_TIMEOUT','模型队列等待超时，尚未开始该次调用。',429)
            try:await asyncio.wait_for(semaphore.acquire(),timeout)
            except TimeoutError:raise ApplicationError('MODEL_QUEUE_TIMEOUT','模型队列等待超时，尚未开始该次调用。',429) from None
            acquired.append(semaphore)
        yield (perf_counter()-started)*1000
    finally:
        for semaphore in reversed(acquired):semaphore.release()


def retryable(error):
    import httpx
    # Timeouts may already have consumed remote work; never replay them implicitly.
    if isinstance(error,(TimeoutError,httpx.TimeoutException)) or getattr(error,'code','') in {'MODEL_TIMEOUT','ROUND_BUDGET_EXCEEDED','MODEL_QUEUE_TIMEOUT','MODEL_CALL_BUDGET_EXCEEDED','MODEL_TOKEN_BUDGET_EXCEEDED'}:return False
    status=getattr(error,'status_code',None)
    if status is None and isinstance(error,httpx.HTTPStatusError):status=error.response.status_code
    return status in {429,502,503} or type(error).__name__=='APIConnectionError'


def summary(observation):
    events=observation.performance;calls=observation.calls
    return {'policy':ServingPolicy.deployed().model_dump(),'cache_hits':sum(e['kind']=='cache_hit' for e in events),
            'cache_misses':sum(e['kind']=='cache_miss' for e in events),
            'cache_errors':sum(e['kind']=='cache_error' for e in events),
            'rewrite_skipped':sum(e['kind']=='rewrite_skip' for e in events),
            'retries':sum(c.get('attempt',1)>1 for c in calls),
            'queue_wait_ms':round(sum(c.get('queue_ms',0) for c in calls),3),
            'observed_chat_attempts':sum(c['kind']=='chat' for c in calls),
            'unknown_usage_calls':sum(c['kind']=='chat' and c['usage']['status']!='available' for c in calls),
            'token_limit_basis':'reported_usage_only; unknown usage is not zero; output caps also applied',
            'events':events[:64]}
