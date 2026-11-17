from contextvars import ContextVar

# Server-created binding only; never populated from a message or request body.
run_attempt=ContextVar('durable_run_attempt',default=None)
