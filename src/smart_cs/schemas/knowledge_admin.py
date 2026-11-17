from pydantic import BaseModel,ConfigDict,Field

class DocumentCommand(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    content_hash:str=Field(pattern=r'^[a-f0-9]{64}$')
    version:str=Field(min_length=1,max_length=32)

class ActivateIndex(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_active_index_id:str|None=Field(max_length=80)
