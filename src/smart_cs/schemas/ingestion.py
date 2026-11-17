"""Local operator ingestion contract; no model or HTTP-supplied actor authority."""
from datetime import date
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel,ConfigDict,Field,model_validator,field_validator


class DocumentSpec(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    source_key: str=Field(min_length=1,max_length=256)
    title: str=Field(min_length=1,max_length=180)
    version: str=Field(min_length=1,max_length=32)
    domain: str=Field(min_length=1,max_length=60)
    access_scope: Literal['public','support','admin','private']
    owner_id: str|None=Field(default=None,min_length=1,max_length=128)
    effective_from: date
    effective_until: date|None=None
    source_type: str=Field(min_length=1,max_length=32)
    publisher: str=Field(min_length=1,max_length=180)
    license: str=Field(min_length=1,max_length=180)
    source_url: str|None=Field(default=None,max_length=2048)
    expected_sha256: str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def owner_scope(self):
        if (self.access_scope=='private')!=(self.owner_id is not None):raise ValueError('Private scope requires exactly one owner; other scopes cannot have owners')
        if self.effective_until is not None and self.effective_until<=self.effective_from:raise ValueError('effective_until must be later than effective_from')
        return self

    @field_validator('source_url')
    @classmethod
    def provenance_url(cls,value):
        if value:
            parsed=urlsplit(value)
            if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError('Use a public HTTP(S) provenance URL')
            # Legal download URLs may have non-secret query parameters.
            from urllib.parse import parse_qsl
            if any(any(word in key.lower() for word in ['token','key','secret','password','signature','credential']) for key,_ in parse_qsl(parsed.query,keep_blank_values=True)):
                raise ValueError('Credential-bearing provenance URLs are forbidden')
        return value


class IngestionOptions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    chunker: Literal['structural','bge-semantic']='structural'
    max_chars: int=Field(default=1000,ge=120,le=4000)
    overlap_chars: int=Field(default=100,ge=0,le=600)
    max_tokens: int=Field(default=360,ge=100,le=1000)
    min_tokens: int=Field(default=100,ge=1,le=500)
    semantic_threshold: float=Field(default=0.65,ge=0,le=1)

    @model_validator(mode='after')
    def limits(self):
        if self.overlap_chars>=self.max_chars:raise ValueError('Overlap must be smaller than chunk size')
        if self.min_tokens>=self.max_tokens:raise ValueError('min_tokens must be smaller than max_tokens')
        return self
