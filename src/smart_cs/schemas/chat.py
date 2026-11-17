from typing import Any
from pydantic import BaseModel,Field,field_validator,ConfigDict


class ChatRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    message: str=Field(min_length=1,max_length=8000)
    user_id: str=Field(default='anonymous',min_length=1,max_length=128,deprecated=True,
                      description='Legacy offline adapter only. Live API ignores this field; identity comes from Bearer authentication.')
    session_id: str|None=Field(default=None,max_length=128)
    client_request_id: str|None=Field(default=None,min_length=16,max_length=100,pattern=r'^[A-Za-z0-9_.:-]+$')

    @field_validator('message','user_id',mode='before')
    @classmethod
    def trim(cls,value):return value.strip() if isinstance(value,str) else value


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    compliance_passed: bool
    classification: dict[str,Any]=Field(default_factory=dict)
    agent_steps: list[str]=Field(default_factory=list)
    agent_models: dict[str,str]=Field(default_factory=dict)
    execution_trace: list[dict[str,Any]]=Field(default_factory=list)
    duration_ms: float=0
    citations: list[dict[str,Any]]=Field(default_factory=list)
    knowledge_result: dict[str,Any]=Field(default_factory=dict)
    action_plan: dict[str,Any]=Field(default_factory=dict)
    action_review: dict[str,Any]=Field(default_factory=dict)
    action_request: dict[str,Any]=Field(default_factory=dict)
    action_execution: dict[str,Any]=Field(default_factory=dict)
    task_plan: dict[str,Any]=Field(default_factory=dict)
    content_review: dict[str,Any]=Field(default_factory=dict)
    request_id: str=''
    trace_id: str=''
    run_id: str=''
    recording_status: str=''
    turn_id: str=''
    graph_version: str=''
    persistence: str=''


class ResumeRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    client_request_id: str|None=Field(default=None,min_length=16,max_length=100,pattern=r'^[A-Za-z0-9_.:-]+$')
