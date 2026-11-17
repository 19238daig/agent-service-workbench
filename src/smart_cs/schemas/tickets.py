from datetime import datetime,timezone
from decimal import Decimal
from typing import Literal,Annotated
from pydantic import BaseModel,ConfigDict,Field,StringConstraints,field_validator

TicketStatus=Literal['created','processing','pending_review','resolved','closed','escalated']
TicketType=Literal['refund','complaint','general','claim','account_open','account_change']
Text=Annotated[str,StringConstraints(strip_whitespace=True,min_length=1,max_length=500)]
TicketId=Annotated[str,StringConstraints(strip_whitespace=True,min_length=1,max_length=40,pattern=r'^[A-Za-z0-9_-]+$')]


class TicketRegistration(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    type: TicketType
    priority: Literal['low','medium','high','urgent']='medium'
    summary: Text
    details: str=Field(default='',max_length=8000)
    order_id: str | None=Field(default=None,min_length=1,max_length=64)
    confirm_registration: Literal[True]

    @field_validator('confirm_registration',mode='before')
    @classmethod
    def explicit_confirmation(cls,value):
        if value is not True:raise ValueError('Explicit boolean confirmation required')
        return value


class TicketTransition(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    target_status: TicketStatus
    expected_version: int=Field(ge=1)
    reason: Text


class TicketAssignment(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    assignee_id: str | None=Field(min_length=1,max_length=128)
    expected_version: int=Field(ge=1)
    reason: Text


class TicketView(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    ticket_id: str=Field(validation_alias='id')
    user_id: str
    order_id: str | None
    type: TicketType
    priority: str
    status: TicketStatus
    summary: str
    details: str
    requested_amount: Decimal | None=None
    source: str
    created_at: datetime
    updated_at: datetime
    version: int
    assignee_id: str | None
    allowed_transitions: list[TicketStatus]=Field(default_factory=list)
    can_assign: bool=False

    @field_validator('created_at','updated_at')
    @classmethod
    def utc_dates(cls,value):return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class TicketEventView(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: str
    action_id: str | None=None
    ticket_id: str
    actor_id: str
    actor_role: str | None
    event_type: str
    from_status: TicketStatus | None
    to_status: TicketStatus
    ticket_version: int | None
    created_at: datetime
    source: str | None
    note: str
    assignee_before: str | None
    assignee_after: str | None

    @field_validator('created_at')
    @classmethod
    def utc_date(cls,value):return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
