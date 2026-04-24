import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

_DATABASE_URL = os.getenv("POSTGRES_DSN", "")

engine = create_async_engine(_DATABASE_URL, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def get_db_session(workspace_id: str | None = None) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        if workspace_id:
            await session.execute(
                text("SELECT set_config('app.current_workspace_id', :wid, true)"),
                {"wid": str(workspace_id)},
            )
        yield session
