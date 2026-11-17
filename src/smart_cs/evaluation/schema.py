from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class Evidence(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source: str
    document_id: str
    source_sha256: str
    version: str
    source_url: str
    page: int | None
    heading: str
    quote: str = Field(min_length=4)
    paragraph_sha256: str
    supporting_text: str


class EvidenceGroup(BaseModel):
    alternatives: list[Evidence] = Field(min_length=1)


class CaseBase(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str
    group_id: str
    split: Literal['train','validation','test']
    message: str = Field(min_length=1)
    provenance: Literal['ai_authored_source_grounded_candidate','ai_authored_synthetic_workflow']
    review_status: Literal['pending_human_review']='pending_human_review'
    capability_status: Literal['current','planned','evaluation_only']='current'
    history: list[dict[str,str]]=Field(default_factory=list)


class RagCase(CaseBase):
    task: Literal['rag']='rag'
    case_type: Literal['direct','multi_evidence','unanswerable','version','injection']
    answerable: bool
    evidence: list[EvidenceGroup]
    reference_points: list[str] = Field(min_length=1)
    forbidden_claims: list[str]=Field(default_factory=list)
    untrusted_context: str | None=None
    evaluation_scope: Literal['retrieval','generation_only']='retrieval'

    @model_validator(mode='after')
    def evidence_required(self):
        if self.answerable and not self.evidence:raise ValueError('Answerable case requires source evidence')
        if not self.answerable and self.evidence:raise ValueError('Unanswerable case must not invent supporting evidence')
        if self.case_type=='multi_evidence' and len(self.evidence)<2:raise ValueError('Multiple independent evidence groups required')
        return self


class IntentCase(CaseBase):
    task: Literal['intent']='intent'
    label: str
    expected_route: Literal['knowledge_rag','ticket_handler','compliance_checker','clarification']
    expected_entities: dict[str,str]=Field(default_factory=dict)


class TicketCase(CaseBase):
    task: Literal['ticket_workflow']='ticket_workflow'
    fixture: dict
    expected_operation: Literal['create','query','reject','clarify']
    expected_effects: dict
    forbidden_effects: list[str]
    contract_refs: list[str]


class ComplianceCase(CaseBase):
    task: Literal['compliance']='compliance'
    candidate_answer: str
    expected_decision: Literal['allow','block']
    reasons: list[str]
    contract_refs: list[str]


Case=Annotated[RagCase|IntentCase|TicketCase|ComplianceCase,Field(discriminator='task')]
CASE_ADAPTER=TypeAdapter(Case)
