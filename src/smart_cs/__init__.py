"""Python multi-agent customer service application."""

# psycopg's async PostgreSQL checkpointer requires Selector on Windows.
# Apply before asyncio.run / unittest / AnyIO create their loops. Uvicorn has
# its own loop factory, explicitly configured by our launcher as well.
import sys
if sys.platform=='win32':
    import asyncio
    if not isinstance(asyncio.get_event_loop_policy(),asyncio.WindowsSelectorEventLoopPolicy):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

__version__ = "1.0.0"
