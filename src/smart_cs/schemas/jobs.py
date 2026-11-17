"""Administrative background work, with no arbitrary function, path or actor."""
from typing import Literal,Annotated
from pydantic import BaseModel,ConfigDict,Field,model_validator
from smart_cs.schemas.ingestion import DocumentSpec,IngestionOptions

class IngestWork(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    kind:Literal['ingest']='ingest'
    artifact_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    extension:Literal['.pdf','.html','.htm','.md','.txt']
    document:DocumentSpec
    options:IngestionOptions=Field(default_factory=IngestionOptions)
    publish:bool=False
    @model_validator(mode='after')
    def artifact_binding(self):
        if self.document.expected_sha256 not in {None,self.artifact_sha256}:raise ValueError('Document checksum must match uploaded artifact')
        return self

class IndexWork(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    kind:Literal['index']='index'

class EvaluationWork(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    kind:Literal['retrieval_evaluation']='retrieval_evaluation'
    split:Literal['train','validation']='validation'

Work=Annotated[IngestWork|IndexWork|EvaluationWork,Field(discriminator='kind')]

class JobRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    idempotency_key:str=Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_.:-]+$')
    work:Work

class JobRetry(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    payload_hash:str=Field(pattern=r'^[a-f0-9]{64}$')
