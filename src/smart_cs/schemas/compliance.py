"""Model verdicts exclude server-owned identity, hashes, approvals and execution."""
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from smart_cs.schemas.actions import PlanValidation

Reason=Literal['ACTION_ALLOWED','CONTENT_ALLOWED','USER_CONFIRMATION_REQUIRED','STAFF_APPROVAL_REQUIRED',
    'VALIDATION_FAILED','MISSING_INFORMATION','BINDING_MISMATCH','ACTION_STALE','INVALID_REVIEW',
    'REVIEW_UNAVAILABLE','SENSITIVE_DATA','UNSAFE_INSTRUCTION','UNSUPPORTED_SUCCESS','UNSUPPORTED_PROMISE',
    'FINANCIAL_GUARANTEE','MODEL_BLOCKED','SCOPE_MISMATCH','READ_ONLY_CHECKED','NO_ACTION']
Risk=Literal['low','medium','high','critical']
Decision=Literal['allow','require_approval','block','clarify']
Approval=Literal['none','user','staff']
ActionReason=Literal['ACTION_ALLOWED','USER_CONFIRMATION_REQUIRED','STAFF_APPROVAL_REQUIRED','MODEL_BLOCKED',
    'SENSITIVE_DATA','UNSAFE_INSTRUCTION','SCOPE_MISMATCH']
ContentReason=Literal['CONTENT_ALLOWED','SENSITIVE_DATA','UNSAFE_INSTRUCTION','UNSUPPORTED_SUCCESS',
    'UNSUPPORTED_PROMISE','FINANCIAL_GUARANTEE','MODEL_BLOCKED','SCOPE_MISMATCH']


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,str_strip_whitespace=True)


class ActionVerdict(Strict):
    decision:Decision
    risk_level:Risk
    reason_codes:list[ActionReason]=Field(min_length=1,max_length=8)
    required_approval:Approval

    @model_validator(mode='after')
    def coherent(self):
        if self.decision=='allow' and (self.risk_level not in {'low','medium'} or self.required_approval!='none' or self.reason_codes!=['ACTION_ALLOWED']):
            raise ValueError('Contradictory allow')
        if self.decision=='require_approval' and (self.required_approval=='none' or self.risk_level=='critical'):
            raise ValueError('Missing approval or critical action')
        if self.decision in {'block','clarify'} and self.required_approval!='none':raise ValueError('Blocked actions cannot request approval')
        if self.risk_level=='critical' and self.decision!='block':raise ValueError('Critical action must block')
        return self


class ContentVerdict(Strict):
    decision:Literal['allow','block']
    risk_level:Risk
    reason_codes:list[ContentReason]=Field(min_length=1,max_length=8)

    @model_validator(mode='after')
    def coherent(self):
        if self.decision=='allow' and (self.risk_level not in {'low','medium'} or self.reason_codes!=['CONTENT_ALLOWED']):
            raise ValueError('Contradictory content allow')
        return self


class ReviewRecord(Strict):
    phase:Literal['action','content']
    decision:Decision
    risk_level:Risk
    reason_codes:list[Reason]
    required_approval:Approval='none'
    protocol:Literal['dual-review-v1']='dual-review-v1'
    rule_version:str
    policy_hash:str
    reviewer:Literal['rules','rules_and_model','fail_closed']
    action_id:str|None=None
    parameter_hash:str|None=None
    binding_hash:str|None=None
    content_sha256:str|None=None
    reviewed_at:str
    execution_authorized:Literal[False]=False


class ReviewedAction(Strict):
    plan:PlanValidation|None=None
    validation_status:str
    missing_fields:list[str]=Field(default_factory=list)
    review:ReviewRecord
