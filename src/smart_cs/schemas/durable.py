from pydantic import BaseModel,ConfigDict,Field,field_validator

class RunSubmission(BaseModel):
    model_config=ConfigDict(extra='forbid')
    client_request_id:str=Field(min_length=16,max_length=100,pattern=r'^[A-Za-z0-9_.:-]+$')
    message:str=Field(min_length=1,max_length=8000)
    session_id:str|None=Field(default=None,min_length=1,max_length=128)
    @field_validator('message',mode='before')
    @classmethod
    def trim(cls,value):return value.strip() if isinstance(value,str) else value

class RunResume(BaseModel):
    model_config=ConfigDict(extra='forbid')
    client_request_id:str=Field(min_length=16,max_length=100,pattern=r'^[A-Za-z0-9_.:-]+$')

class RunCancellation(BaseModel):
    model_config=ConfigDict(extra='forbid')
