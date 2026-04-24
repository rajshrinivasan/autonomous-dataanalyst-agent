import os

from langgraph.checkpoint.memory import MemorySaver

try:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    _HAS_POSTGRES_SAVER = True
except ImportError:
    _HAS_POSTGRES_SAVER = False


async def get_checkpointer():
    """Return AsyncPostgresSaver when POSTGRES_DSN is set, else MemorySaver.

    AsyncPostgresSaver requires: pip install langgraph-checkpoint-postgres psycopg[binary]
    """
    postgres_dsn = os.getenv("POSTGRES_DSN")
    if postgres_dsn and _HAS_POSTGRES_SAVER:
        saver = AsyncPostgresSaver.from_conn_string(postgres_dsn)
        await saver.setup()
        return saver
    return MemorySaver()
