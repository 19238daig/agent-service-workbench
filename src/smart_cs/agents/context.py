"""Shared context and execution-path helpers for graph nodes."""
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage


def latest_user_turn(messages: list[BaseMessage]) -> tuple[str, list[BaseMessage]]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, HumanMessage):
            return str(message.content), messages[max(0, index - 6):index]
    return "", []


def append_step(state: dict[str, Any], node: str) -> list[str]:
    return [*state.get("agent_steps", []), node]


def record_model(state: dict[str, Any], agent: str, model, *, reset: bool = False) -> dict[str, str]:
    """Public execution metadata; never include credentials or provider URLs."""
    name = getattr(model, "model_name", None) or type(model).__name__
    return {**({} if reset else state.get("agent_models", {})), agent: str(name)}
