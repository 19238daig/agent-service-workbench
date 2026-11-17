from contextvars import ContextVar

GRAPH_VERSION='smart-cs-readonly-v2'
active_saver=ContextVar('active_checkpoint_saver',default=None)
execution_binding=ContextVar('trusted_execution_binding',default=None)
