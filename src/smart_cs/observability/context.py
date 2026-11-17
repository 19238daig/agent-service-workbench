from contextvars import ContextVar
from dataclasses import dataclass,field
from uuid import uuid4
from time import perf_counter

request_context=ContextVar('smart_cs_request',default=None)
run_context=ContextVar('smart_cs_run',default=None)
operation_context=ContextVar('smart_cs_operation',default='unknown')


@dataclass
class RequestCorrelation:
    request_id: str=field(default_factory=lambda:uuid4().hex)
    trace_id: str=field(default_factory=lambda:uuid4().hex)
    run_id: str|None=None


@dataclass
class RunObservation:
    run_id: str
    request_id: str
    trace_id: str
    calls: list=field(default_factory=list)
    sink: object=None
    performance: list=field(default_factory=list)
    started: float=field(default_factory=perf_counter)
    chat_attempts: int=0


def correlation():return request_context.get() or RequestCorrelation()


def usage_summary(calls):
    chat=[c for c in calls if c['kind']=='chat']
    if not chat:return {'status':'not_applicable','chat_calls':0,'unknown_usage_calls':0,'input_tokens':None,'output_tokens':None,'total_tokens':None}
    unknown=sum(c['usage']['status']=='unknown' for c in chat)
    result={'status':'unknown' if unknown==len(chat) else 'available' if all(c['usage']['status']=='available' for c in chat) else 'partial',
            'chat_calls':len(chat),'unknown_usage_calls':unknown}
    for key in ['input_tokens','output_tokens','total_tokens']:
        values=[c['usage'][key] for c in chat]
        result[key]=sum(values) if all(v is not None for v in values) else None
    return result
