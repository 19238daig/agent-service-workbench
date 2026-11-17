"""Per-agent model configuration, with a backwards-compatible shared default."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Mapping

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI


AGENT_MODEL_PREFIXES = {
    'intent_router': 'INTENT',
    'knowledge_rag': 'KNOWLEDGE',
    'ticket_handler': 'TICKET',
    'compliance_checker': 'COMPLIANCE',
}


@dataclass(frozen=True)
class AgentModelSettings:
    model_name: str
    base_url: str | None
    api_key: str | None = field(repr=False)
    temperature: float = 0
    timeout: float = 60
    use_system_proxy: bool = False


def resolve_agent_model_settings(agent: str, environ: Mapping[str, str] | None = None) -> AgentModelSettings:
    env = os.environ if environ is None else environ
    prefix = AGENT_MODEL_PREFIXES[agent]

    def value(name: str, fallback: str = '') -> str:
        return (env.get(name) or '').strip() or fallback

    model_name = value(f'{prefix}_MODEL_NAME', value('MODEL_NAME', 'gpt-4o'))
    role_key = value(f'{prefix}_OPENAI_API_KEY')
    role_url = value(f'{prefix}_OPENAI_BASE_URL')
    common_url = value('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    if role_url and role_url.rstrip('/') != common_url.rstrip('/') and not role_key:
        raise ValueError(f'{prefix}_OPENAI_API_KEY is required for a different provider endpoint')
    api_key = role_key or value('OPENAI_API_KEY') or None
    base_url = role_url or value('OPENAI_BASE_URL') or None
    try:
        temperature = float(value(f'{prefix}_TEMPERATURE', '0'))
        timeout = float(value(f'{prefix}_TIMEOUT', value('MODEL_TIMEOUT', '60')))
    except ValueError:
        raise ValueError(f'{prefix}: temperature and timeout must be numbers') from None
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError(f'{prefix}_TEMPERATURE must be between 0 and 2')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f'{prefix}_TIMEOUT must be a positive number')
    return AgentModelSettings(model_name, base_url, api_key, temperature, timeout,
                              value('MODEL_USE_SYSTEM_PROXY', 'false').lower() == 'true')


def model_credentials_configured() -> bool:
    return all(resolve_agent_model_settings(agent).api_key not in {None, 'sk-your-openai-api-key'}
               for agent in AGENT_MODEL_PREFIXES)


def build_agent_models(
    default_model: BaseChatModel | None = None,
    overrides: Mapping[str, BaseChatModel] | None = None,
) -> dict[str, BaseChatModel]:
    """Explicit role override → injected shared model → role/global env settings."""
    from smart_cs.performance.policy import ServingPolicy
    policy=ServingPolicy.deployed()
    overrides = overrides or {}
    unknown = set(overrides) - set(AGENT_MODEL_PREFIXES)
    if unknown:
        raise ValueError('Unknown agent model roles: ' + ', '.join(sorted(unknown)))
    cache: dict[AgentModelSettings, BaseChatModel] = {}
    models = {}
    for agent in AGENT_MODEL_PREFIXES:
        if agent in overrides:
            models[agent] = overrides[agent]
        elif default_model is not None:
            models[agent] = default_model
        else:
            settings = resolve_agent_model_settings(agent)
            if settings not in cache:
                cache[settings] = ChatOpenAI(
                    model=settings.model_name, api_key=settings.api_key,
                    base_url=settings.base_url, temperature=settings.temperature,
                    timeout=settings.timeout, max_retries=0 if policy.enabled else 1,
                    # Graph event observers must not switch structured model calls
                    # to provider token streaming. The UI streams node facts only.
                    disable_streaming=True,
                    http_client=httpx.Client(trust_env=settings.use_system_proxy),
                    http_async_client=httpx.AsyncClient(trust_env=settings.use_system_proxy),
                )
            models[agent] = cache[settings]
    # Reuse only clients built from their own configured endpoint/credentials.
    # No runtime failover or routing to an arbitrary provider is permitted.
    return {role:models[policy.role_sources.get(role,role)] if policy.enabled and role not in overrides and default_model is None else models[role] for role in models}
