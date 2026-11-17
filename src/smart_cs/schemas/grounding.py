"""Small, strict model contracts. Source identifiers and URLs remain server-owned."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class QuoteReference(StrictContract):
    chunk_id: str = Field(min_length=1, max_length=160)
    quote: str = Field(min_length=8, max_length=1200)


class GroundedClaim(StrictContract):
    text: str = Field(min_length=1, max_length=700)
    references: list[QuoteReference] = Field(min_length=1, max_length=4)


class AnswerDraft(StrictContract):
    status: Literal['answered', 'insufficient', 'conflict']
    claims: list[GroundedClaim] = Field(max_length=6)

    @model_validator(mode='after')
    def claims_match_status(self):
        if bool(self.claims) != (self.status == 'answered'):
            raise ValueError('Only an answered draft must contain claims')
        return self


class ClaimReview(StrictContract):
    claim: int = Field(ge=1, le=6)
    supported: bool


class SupportReview(StrictContract):
    status: Literal['supported', 'insufficient', 'conflict', 'not_current']
    answers_question: bool
    scope_valid: bool
    claims: list[ClaimReview] = Field(max_length=6)
