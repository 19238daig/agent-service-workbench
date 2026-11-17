from typing import Literal,Any
from pydantic import BaseModel,Field
from smart_cs.schemas.identity import IdentityContext


class TokenUsage(BaseModel):
    input_tokens: int|None=Field(default=None,ge=0)
    output_tokens: int|None=Field(default=None,ge=0)
    total_tokens: int|None=Field(default=None,ge=0)
    status: Literal['available','partial','unknown']
    source: Literal['langchain_usage_metadata','provider_response_metadata','missing']


class ModelCall(BaseModel):
    id: str
    run_id: str
    request_id: str
    trace_id: str
    kind: Literal['chat','embedding','reranker']
    agent_role: str
    operation: str
    model: str
    status: Literal['succeeded','failed','cancelled']
    error_code: str|None
    span_id: str|None
    duration_ms: float=Field(ge=0)
    usage: TokenUsage
    attempt: int=1
    queue_ms: float=0
    response_model: str|None=None


class RunRecord(BaseModel):
    run_id: str
    request_id: str
    trace_id: str
    session_id: str
    identity: IdentityContext
    status: Literal['queued','running','succeeded','blocked','failed','cancelled','waiting_approval','interrupted']
    recording_backend: Literal['database','process_memory']
    duration_ms: float|None
    error_code: str|None
    nodes: list[dict[str,Any]]
    usage: dict[str,Any]
    model_calls: list[ModelCall]
    turn_id: str|None=None
    graph_version: str|None=None
    execution_kind: str='new'
    resumed_from_run_id: str|None=None
    durability: str|None=None
    last_event_id: int|None=None
    run_status: str|None=None
    can_subscribe: bool=False
    result_event: dict[str,Any]|None=None
    performance: dict[str,Any]=Field(default_factory=dict)
