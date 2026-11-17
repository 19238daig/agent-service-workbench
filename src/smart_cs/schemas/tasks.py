"""Finite task proposals contain neither dependencies nor execution authority."""
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from smart_cs.schemas.orders import OrderId,Money
from smart_cs.schemas.tickets import TicketId,TicketStatus

PROTOCOL='finite-tasks-v1'
TaskKind=Literal['policy_lookup','order_query','order_eligibility','ticket_query','ticket_create','ticket_transition']
TaskStatus=Literal['pending','running','completed','needs_information','waiting_approval','blocked']


class Slots(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,str_strip_whitespace=True)
    order_id:OrderId|None=None
    ticket_id:TicketId|None=None
    requested_amount:Money|None=None
    type:Literal['refund','complaint','general']|None=None
    priority:Literal['low','medium','high','urgent']|None=None
    summary:str|None=Field(default=None,min_length=1,max_length=500)
    details:str|None=Field(default=None,min_length=1,max_length=8000)
    target_status:TicketStatus|None=None
    expected_version:int|None=Field(default=None,ge=1)
    reason:str|None=Field(default=None,min_length=1,max_length=500)


class TaskProposal(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    tasks:list[TaskKind]=Field(min_length=1,max_length=6)
    slots:Slots=Field(default_factory=Slots)


class Task(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    id:str
    kind:TaskKind
    depends_on:list[str]=Field(default_factory=list)
    status:TaskStatus='pending'
    reason_code:str=''
    result:dict=Field(default_factory=dict)


class TaskPlan(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    protocol:Literal['finite-tasks-v1']='finite-tasks-v1'
    id:str
    actor_id:str
    session_id:str
    context_hash:str
    goal:str=Field(max_length=8000)
    replies:list[str]=Field(default_factory=list,max_length=6)
    slots:Slots=Field(default_factory=Slots)
    tasks:list[Task]=Field(min_length=1,max_length=6)
    status:Literal['running','needs_information','waiting_approval','completed','blocked']='running'
    missing_fields:list[str]=Field(default_factory=list)
    steps_used:int=Field(default=0,ge=0)
    max_steps:int=Field(default=8,ge=1,le=8)
    clarification_rounds:int=Field(default=0,ge=0,le=6)
    active_task_id:str|None=None


def public_plan(raw):
    if not raw:return {}
    plan=TaskPlan.model_validate(raw)
    return {'protocol':plan.protocol,'plan_id':plan.id,'status':plan.status,
        'tasks':[{'id':t.id,'kind':t.kind,'depends_on':t.depends_on,'status':t.status,'reason_code':t.reason_code} for t in plan.tasks],
        'completed_task_ids':[t.id for t in plan.tasks if t.status=='completed'],
        'missing_fields':plan.missing_fields,'steps_used':plan.steps_used,'max_steps':plan.max_steps,
        'clarification_rounds':plan.clarification_rounds}
