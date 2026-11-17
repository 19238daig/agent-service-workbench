from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

PROTOCOL='action-execution-v1'


class ExecutionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    parameter_hash:str=Field(pattern=r'^[a-f0-9]{64}$')


class ExecutionResult(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    protocol:Literal['action-execution-v1']='action-execution-v1'
    action_id:str
    parameter_hash:str
    draft_hash:str
    tool:Literal['ticket_create','ticket_transition']
    status:Literal['succeeded']='succeeded'
    ticket_id:str
    ticket_version:int=Field(ge=1)
    ticket_status:str
    event_id:str
    committed_at:str
    requested_amount:str|None=None
    reservation_change:str
    funds_refunded:Literal[False]=False
