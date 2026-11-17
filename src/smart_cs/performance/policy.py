import hashlib
import json
import os
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from smart_cs.schemas.errors import ApplicationError

PROFILE = Path(__file__).with_name('profile.json')
PROTOCOL = 'bounded-serving-v1'
ROLES = {'intent_router','knowledge_rag','ticket_handler','compliance_checker'}


class ServingPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True,frozen=True)
    enabled: bool = True
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=120,ge=1,le=600)
    cache_max_entries: int = Field(default=256,ge=1,le=1024)
    rewrite_on_demand: bool = True
    global_concurrency: int = Field(default=4,ge=1,le=16)
    role_concurrency: int = Field(default=2,ge=1,le=8)
    local_concurrency: int = Field(default=2,ge=1,le=4)
    queue_seconds: float = Field(default=10.,gt=0,le=60,allow_inf_nan=False)
    call_seconds: float = Field(default=45.,gt=0,le=120,allow_inf_nan=False)
    round_seconds: float = Field(default=180.,gt=0,le=600,allow_inf_nan=False)
    max_chat_attempts: int = Field(default=16,ge=1,le=32)
    reported_token_budget: int = Field(default=50000,ge=1,le=200000)
    max_output_tokens: int = Field(default=3072,ge=128,le=8192)
    max_retries: int = Field(default=1,ge=0,le=2)
    role_sources: dict[str,str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def routes(self):
        if any(k not in ROLES or v not in ROLES for k,v in self.role_sources.items()):
            raise ValueError('Only configured role clients may be reused')
        return self

    @classmethod
    def deployed(cls):
        try:
            value=json.loads(PROFILE.read_text(encoding='utf-8'))
            for name,key in [('PERF_ENABLED','enabled'),('PERF_CACHE_ENABLED','cache_enabled')]:
                if name in os.environ:
                    flag=os.environ[name].lower()
                    if flag not in {'true','false'}:raise ValueError('Invalid boolean')
                    value[key]=flag=='true'
            return cls.model_validate(value)
        except (OSError,ValueError):raise ApplicationError('SERVING_POLICY_INVALID','运行预算配置无效。',503) from None


def identity():
    return {'protocol':PROTOCOL,'policy':ServingPolicy.deployed().model_dump(),
            'rewrite':'referential-or-short-with-history-v1','cache':'signed-canonical-evidence-v1'}


def fingerprint():
    return hashlib.sha256(json.dumps(identity(),sort_keys=True,separators=(',',':')).encode()).hexdigest()
