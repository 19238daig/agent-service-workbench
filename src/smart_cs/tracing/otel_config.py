"""
OpenTelemetry Span 与安全的请求/运行关联。
Token 用量由 observability.models 从实际供应商元数据读取。
可配置 OTLP 导出；控制台导出需要显式启用。
"""

from __future__ import annotations

import functools
import time
import os
from typing import Any, Callable

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


_tracer = None


def init_tracer(
    service_name: str = "smart-cs-multi-agent",
    otlp_endpoint: str | None = None,
) -> None:
    """
    初始化OpenTelemetry追踪器。

    Args:
        service_name: 服务名称
        otlp_endpoint: OTLP收集器地址；未设置时仅在显式配置下输出控制台
    """
    global _tracer

    if not _HAS_OTEL or getattr(init_tracer, '_initialized', False):
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        except ImportError:
            exporter = ConsoleSpanExporter()
    else:
        exporter = ConsoleSpanExporter() if os.getenv('OTEL_CONSOLE_EXPORT','false').lower()=='true' else None

    if exporter is not None:provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    init_tracer._initialized = True


def get_tracer():
    """获取全局Tracer实例"""
    global _tracer
    if _tracer is None:
        if _HAS_OTEL:
            _tracer = trace.get_tracer("smart-cs-multi-agent")
        else:
            return None
    return _tracer


def trace_agent_call(agent_name: str) -> Callable:
    """
    Agent调用追踪装饰器。

    为每个Agent方法创建一个Span，记录：
    - agent.name: Agent名称
    - agent.duration_ms: 调用耗时
    - agent.input_size: 输入大小
    - agent.success: 是否成功

    用法：
        @trace_agent_call("knowledge_rag")
        async def process(self, state):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            from smart_cs.observability.context import operation_context, run_context
            from smart_cs.schemas.errors import classify_failure
            tracer = get_tracer()

            from contextlib import nullcontext

            span_name = f"agent.{agent_name}.{func.__name__}"

            manager=tracer.start_as_current_span(span_name,record_exception=False,set_status_on_exception=False) if tracer else nullcontext(None)
            with manager as span:
                if span:
                    span.set_attribute("agent.name", agent_name)
                    span.set_attribute("agent.method", func.__name__)
                    observation=run_context.get()
                    if observation:
                        span.set_attribute('app.run_id',observation.run_id)
                        span.set_attribute('app.request_id',observation.request_id)
                        span.set_attribute('app.trace_id',observation.trace_id)
                token=operation_context.set(agent_name)

                start_time = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    duration_ms = (time.perf_counter() - start_time) * 1000

                    if span:
                        span.set_attribute("agent.duration_ms", duration_ms)
                        span.set_attribute("agent.success", True)

                    if span and isinstance(result, dict):
                        span.set_attribute("agent.result_keys", str(list(result.keys())))

                    return result

                except BaseException as e:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    if span:
                        span.set_attribute("agent.duration_ms", duration_ms)
                        span.set_attribute("agent.success", False)
                        span.set_attribute("error.code", classify_failure(e)[0])
                    raise
                finally:
                    operation_context.reset(token)

        return wrapper
    return decorator


class AgentMetrics:
    """Agent调用指标收集器"""

    def __init__(self):
        self._call_counts: dict[str, int] = {}
        self._total_duration: dict[str, float] = {}
        self._error_counts: dict[str, int] = {}

    def record_call(self, agent_name: str, duration_ms: float, success: bool):
        self._call_counts[agent_name] = self._call_counts.get(agent_name, 0) + 1
        self._total_duration[agent_name] = self._total_duration.get(agent_name, 0.0) + duration_ms
        if not success:
            self._error_counts[agent_name] = self._error_counts.get(agent_name, 0) + 1

    def get_summary(self) -> dict[str, Any]:
        summary = {}
        for agent_name in self._call_counts:
            calls = self._call_counts[agent_name]
            total_ms = self._total_duration[agent_name]
            errors = self._error_counts.get(agent_name, 0)
            summary[agent_name] = {
                "total_calls": calls,
                "avg_duration_ms": total_ms / calls if calls > 0 else 0,
                "error_rate": errors / calls if calls > 0 else 0,
            }
        return summary
