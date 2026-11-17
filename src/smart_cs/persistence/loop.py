import asyncio


def loop_factory():
    """Compatible with psycopg async on Windows and the Linux deployment."""
    return asyncio.SelectorEventLoop()
