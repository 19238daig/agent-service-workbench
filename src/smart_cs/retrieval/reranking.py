"""Pinned local cross-encoder, bounded evidence budget and fail-closed revalidation."""
import hashlib
import json
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from smart_cs.observability.models import local_model
from smart_cs.retrieval.endpoints import validate_local_endpoint
from smart_cs.schemas.errors import ApplicationError

PROFILE = Path(__file__).parent / 'resources/rerank_profile.json'
PROTOCOL = 'bounded-cross-encoder-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class RerankConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    enabled: bool = True
    candidate_k: int = Field(default=20, ge=1, le=50)
    context_k: int = Field(default=3, ge=1, le=10)
    max_length: Literal[256, 512] = 512
    batch_size: Literal[1, 4] = 4
    min_score: float = Field(default=.1, ge=0, le=1, allow_inf_nan=False)
    expected_identity: str = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('max_length', 'batch_size', mode='before')
    @classmethod
    def integer_literals(cls, value):
        if type(value) is not int:
            raise ValueError('Integer settings required')
        return value

    @model_validator(mode='after')
    def bounded(self):
        if self.context_k > self.candidate_k:
            raise ValueError('Evidence count exceeds candidate count')
        return self

    @classmethod
    def deployed(cls):
        try:
            config = cls.model_validate_json(PROFILE.read_text(encoding='utf-8'))
            values = config.model_dump()
            # Preserve the user's existing root .env threshold override.
            if 'RAG_RERANK_MIN_SCORE' in os.environ:
                values['min_score'] = float(os.environ['RAG_RERANK_MIN_SCORE'])
            if 'RAG_RERANK_ENABLED' in os.environ:
                value = os.environ['RAG_RERANK_ENABLED'].lower()
                if value not in {'true', 'false'}:
                    raise ValueError('Invalid enable flag')
                values['enabled'] = value == 'true'
            return cls.model_validate(values)
        except (OSError, ValueError, ValidationError):
            raise ApplicationError('RERANK_CONFIG_INVALID', '重排序配置无效，请检查已验证配置。', 503) from None


def pipeline_identity():
    return {'protocol': PROTOCOL, 'profile': RerankConfig.deployed().model_dump(),
            'passage_template': 'heading-newline-content-v1', 'scores_are_confidence': False}


def validate_identity(value, expected=None):
    try:
        contract = value['contract']
        files = contract['files_sha256']
        valid = (value['sha256'] == digest(contract) and contract['protocol'] == 'bge-cross-encoder-v2'
                 and contract['model'] == 'bge-reranker-v2-m3'
                 and contract['normalization'] == 'sigmoid-float32-logits'
                 and contract['local_files_only'] is True and contract['trust_remote_code'] is False
                 and isinstance(files, dict) and files and all(isinstance(n, str) for n in files)
                 and any(n.endswith(('.safetensors', '.bin')) for n in files)
                 and all(isinstance(h, str) and len(h) == 64 and all(c in '0123456789abcdef' for c in h) for h in files.values()))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ApplicationError('RERANK_IDENTITY_INVALID', '重排序服务身份契约无效。', 503)
    if expected is not None and value['sha256'] != expected:
        raise ApplicationError('RERANK_IDENTITY_CHANGED', '重排序模型或推理契约已变化，请重新验证配置。', 503)
    return value


class CrossEncoderReranker:
    def __init__(self, config=None, *, url=None, transport=None):
        self.config = config or RerankConfig.deployed()
        self.url = validate_local_endpoint(url or os.getenv('RAG_RERANKER_URL', 'http://127.0.0.1:61536'))
        self.transport = transport

    async def identity(self, client):
        response = await client.get(self.url + '/identity')
        response.raise_for_status()
        return validate_identity(response.json(), self.config.expected_identity)

    @local_model('reranker')
    async def score(self, query, passages):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ApplicationError('RERANK_INPUT_INVALID', '重排序查询无效。', 400)
        if not 1 <= len(passages) <= 50 or any(not isinstance(p, str) or not p or len(p) > 12000 for p in passages):
            raise ApplicationError('RERANK_INPUT_INVALID', '重排序候选无效或超过容量。', 400)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=120, transport=self.transport) as client:
                await self.identity(client)
                response = await client.post(self.url + '/rerank/v2', json={
                    'query': query, 'passages': passages, 'max_length': self.config.max_length,
                    'batch_size': self.config.batch_size, 'expected_identity': self.config.expected_identity})
                response.raise_for_status()
                result = response.json()
                await self.identity(client)
            scores, lengths = result['scores'], result['input_pair_tokens_before_truncation']
            valid = (result['identity_sha256'] == self.config.expected_identity and result['normalized'] is True
                     and type(result['max_length']) is int and result['max_length'] == self.config.max_length
                     and type(result['batch_size']) is int and result['batch_size'] == self.config.batch_size
                     and type(result['pairs']) is int and result['pairs'] == len(passages)
                     and isinstance(scores, list) and len(scores) == len(passages)
                     and all(type(s) in {int, float} and math.isfinite(s) and 0 <= s <= 1 for s in scores)
                     and isinstance(lengths, list) and len(lengths) == len(passages)
                     and all(type(n) is int and n > 0 for n in lengths)
                     and type(result['truncated_pairs']) is int
                     and result['truncated_pairs'] == sum(n > self.config.max_length for n in lengths)
                     and type(result['duration_ms']) in {int, float}
                     and math.isfinite(result['duration_ms']) and result['duration_ms'] >= 0)
            if not valid:
                raise ValueError('Invalid score contract')
            return result
        except httpx.TimeoutException:
            raise ApplicationError('RERANK_TIMEOUT', '本地重排序超时，本次未发布答案。', 504) from None
        except httpx.HTTPStatusError as error:
            code = 'RERANK_IDENTITY_CHANGED' if error.response.status_code == 409 else 'RERANK_UNAVAILABLE'
            raise ApplicationError(code, '本地重排序服务不可用或身份已变化。', 503) from None
        except httpx.HTTPError:
            raise ApplicationError('RERANK_UNAVAILABLE', '本地重排序服务暂时不可用。', 503) from None
        except (KeyError, TypeError, ValueError):
            raise ApplicationError('RERANK_RESPONSE_INVALID', '重排序结果未通过契约校验。', 503) from None

    async def rank(self, query, documents, *, revalidate):
        # No passage reaches a model until the original principal is rechecked.
        await revalidate(documents)
        seen = {}
        for doc in documents:
            identifier = doc['id']
            if identifier in seen and seen[identifier] != doc:
                raise ApplicationError('RERANK_DUPLICATE_CONFLICT', '重复切片内容不一致。', 409)
            seen.setdefault(identifier, doc)
        candidates = list(seen.values())[:self.config.candidate_k]
        start = perf_counter()
        audit = {'protocol': PROTOCOL, 'profile': self.config.model_dump(), 'input_count': len(candidates),
                 'scores_are_confidence': False, 'model_calls': 0, 'truncated_pairs': 0}
        if candidates and self.config.enabled:
            result = await self.score(query, [(d['metadata'].get('heading') or '')+'\n'+d['content'] for d in candidates])
            ranked = sorted([{**doc, 'rerank_score': score} for doc, score in zip(candidates, result['scores'])],
                            key=lambda d: (-d['rerank_score'], d['id']))
            filtered = [d for d in ranked if d['rerank_score'] >= self.config.min_score]
            audit.update(model_calls=1, truncated_pairs=result['truncated_pairs'],
                         input_pair_tokens_before_truncation=result['input_pair_tokens_before_truncation'],
                         server_duration_ms=result['duration_ms'])
        else:
            ranked = filtered = candidates
        await revalidate(candidates)
        selected = filtered[:self.config.context_k]
        audit.update(duration_ms=(perf_counter()-start)*1000, output_count=len(selected),
                     context_characters=sum(len(d['content']) for d in selected))
        return {'documents': selected, 'ranked_candidates': ranked, 'audit': audit}
