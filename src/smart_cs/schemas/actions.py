"""Plans express intent, never identity, confirmation or permission to execute."""
import hashlib
import json
from typing import Annotated,Literal,Union
from pydantic import BaseModel,ConfigDict,Field,StringConstraints
from smart_cs.schemas.orders import OrderId,Money
from smart_cs.schemas.tickets import TicketId,TicketStatus

PROTOCOL='action-plan-v1'


class Contract(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,str_strip_whitespace=True)


class PageArguments(Contract):
    limit:int=Field(default=50,ge=1,le=100)
    offset:int=Field(default=0,ge=0)


class OrderArguments(PageArguments):
    order_id:OrderId|None=None


class EligibilityArguments(Contract):
    order_id:OrderId
    requested_amount:Money|None=None


class TicketArguments(Contract):
    ticket_id:TicketId


class TicketListArguments(PageArguments):
    assigned:bool=False
    status:TicketStatus|None=None


class TicketEventsArguments(PageArguments):
    ticket_id:TicketId


OptionalText=Annotated[str,StringConstraints(min_length=1,max_length=500)]


class CreateArguments(Contract):
    type:Literal['refund','complaint','general']
    order_id:OrderId|None=None
    requested_amount:Money|None=None
    priority:Literal['low','medium','high','urgent']='medium'
    summary:OptionalText|None=None
    details:str|None=Field(default=None,min_length=1,max_length=8000)


class TransitionArguments(Contract):
    ticket_id:TicketId
    target_status:TicketStatus
    expected_version:int=Field(ge=1)
    reason:OptionalText


class OrderPlan(Contract):
    name:Literal['order_query']
    arguments:OrderArguments


class EligibilityPlan(Contract):
    name:Literal['order_eligibility']
    arguments:EligibilityArguments


class TicketPlan(Contract):
    name:Literal['ticket_query']
    arguments:TicketArguments


class TicketListPlan(Contract):
    name:Literal['ticket_list']
    arguments:TicketListArguments


class TicketEventsPlan(Contract):
    name:Literal['ticket_events']
    arguments:TicketEventsArguments


class CreatePlan(Contract):
    name:Literal['ticket_create']
    arguments:CreateArguments


class TransitionPlan(Contract):
    name:Literal['ticket_transition']
    arguments:TransitionArguments


PlannedTool=Annotated[Union[OrderPlan,EligibilityPlan,TicketPlan,TicketListPlan,TicketEventsPlan,CreatePlan,TransitionPlan],Field(discriminator='name')]


class ActionPlan(Contract):
    protocol:Literal['action-plan-v1']='action-plan-v1'
    action:PlannedTool


class PlanValidation(Contract):
    protocol:Literal['action-plan-v1']='action-plan-v1'
    action_id:str
    actor_id:str
    tool:str
    read_only:bool
    status:Literal['read_ready','draft_validated','needs_information','rejected']
    parameters:dict
    parameter_hash:str
    reason_codes:list[str]
    missing_fields:list[str]=Field(default_factory=list)
    fact_versions:dict[str,int]=Field(default_factory=dict)
    effects:dict[str,str|bool]=Field(default_factory=dict)
    policy_version:str|None=None
    validated_at:str
    execution_enabled:bool=False
    executed:bool=False


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def contract_fingerprint():return canonical_hash({'protocol':PROTOCOL,'schema':ActionPlan.model_json_schema(),'writes_enabled':False})


def decode_plan(text):
    if not isinstance(text,str) or len(text)>40000:raise ValueError('Invalid plan response')
    text=text.strip()
    if text.startswith('```json\n') and text.endswith('\n```'):text=text[8:-4]
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    return ActionPlan.model_validate(json.loads(text,object_pairs_hook=unique))
