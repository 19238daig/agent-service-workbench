"""Human decisions contain no identity, draft updates or execution authority."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class HumanDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    parameter_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['approve', 'reject']
    note: str = Field(default='', max_length=500)


class ActionCancellation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    parameter_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
