from typing import Literal
from pydantic import BaseModel,ConfigDict,Field


class IdentityContext(BaseModel):
    """Immutable server principal; legacy local mode is for offline adapters only."""
    model_config=ConfigDict(frozen=True,extra='forbid',str_strip_whitespace=True)
    subject_id: str=Field(min_length=1,max_length=128)
    mode: Literal['local_workspace','authenticated_session']='local_workspace'
    authenticated: bool=False
    role: Literal['customer','support','admin']='customer'
    auth_session_id: str | None=None
