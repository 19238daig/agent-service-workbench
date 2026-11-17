"""Release-two candidates; review decisions live outside the immutable release."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from smart_cs.evaluation.schema import RagCase, IntentCase, ComplianceCase


class Lineage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    family: str
    base_message: str
    origin: str
    variant: int=Field(ge=0)
    legacy_split: Literal['train','validation','test']|None=None
    legacy_case: str|None=None
    label_change: str|None=None


class RagV2(RagCase):
    lineage: Lineage
    control: dict=Field(default_factory=dict)


class IntentV2(IntentCase):
    lineage: Lineage
    required_tasks: list[str]=Field(default_factory=list)
    allowed_routes: list[str]=Field(default_factory=list)


class ComplianceV2(ComplianceCase):
    lineage: Lineage
    state: dict=Field(default_factory=dict)
    paired_family: str


class TicketV2(BaseModel):
    model_config=ConfigDict(extra='forbid')
    task: Literal['ticket_workflow']='ticket_workflow'
    id: str
    group_id: str
    split: Literal['train','validation','test']='train'
    message: str
    provenance: Literal['executable_contract_scenario']='executable_contract_scenario'
    review_status: Literal['pending_human_review']='pending_human_review'
    lineage: Lineage
    test_module: str=Field(pattern=r'^test_[a-z0-9_]+$')
    test_class: str=Field(pattern=r'^[A-Za-z0-9_]+$')
    test_method: str=Field(pattern=r'^test_[a-z0-9_]+$')
    contract_refs: list[str]
    expected: Literal['test_assertions_hold']='test_assertions_hold'
    live_agent_quality: Literal[False]=False


ADAPTER=TypeAdapter(Annotated[RagV2|IntentV2|ComplianceV2|TicketV2,Field(discriminator='task')])
