from typing import Any,Literal
from pydantic import BaseModel,Field


class DependencyCheck(BaseModel):
    status: Literal['ready','failed','not_checked','not_applicable']
    required: bool
    code: str|None=None
    duration_ms: float=Field(default=0,ge=0)
    details: dict[str,Any]=Field(default_factory=dict)


class DependencyReport(BaseModel):
    schema_version: int=1
    checked_at: str
    profile: Literal['connectivity','deep']
    ready: bool
    host_checks_included: bool=False
    scope: Literal['infrastructure_not_chat_provider_health']='infrastructure_not_chat_provider_health'
    checks: dict[str,DependencyCheck]
