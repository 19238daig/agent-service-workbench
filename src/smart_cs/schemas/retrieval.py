from typing import Literal,Any
from pydantic import BaseModel,ConfigDict,Field,field_validator


class RetrievalRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    query: str=Field(min_length=1,max_length=2000)
    top_k: int=Field(default=5,ge=1,le=50)
    collections: list[Literal['consumer_reference','helpdesk_reference']]=Field(default_factory=lambda:['consumer_reference','helpdesk_reference'],min_length=1,max_length=2)
    mode: Literal['dense','hybrid']|None=None
    @field_validator('collections')
    @classmethod
    def unique(cls,value):return list(dict.fromkeys(value))


class RetrievalResponse(BaseModel):
    index_id: str
    search: Literal['exact_cosine','hybrid_rrf']='exact_cosine'
    as_of: str
    embedding: str
    documents: list[dict[str,Any]]
    evidence_only: bool=True
    retrieval: dict[str,Any]|None=None
