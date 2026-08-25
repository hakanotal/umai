"""Async engine and session factory.

One session per request or per handler, never a module-level global. This bites
everyone once, so the only exported way to get a session is a context manager
that owns its lifetime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from umai.config.settings import Settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_factory() -> async_sessionmaker[AsyncSession]:
    """The session factory, resolved at call time.

    Deliberately a function. `from umai.db.session import _factory` binds the
    name to whatever it holds at import time — None — and `init_engine`
    rebinding the module global does not update that copy, so the scheduler was
    handed a None factory and the evening summary raised
    `TypeError: 'NoneType' object is not callable` at 21:30 every night.
    """
    if _factory is None:
        raise RuntimeError("init_engine() has not been called")
    return _factory


def init_engine(settings: Settings) -> AsyncEngine:
    """Called once at startup, from the composition root."""
    global _engine, _factory
    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        # One always-on instance does not need a large pool, and a small one makes
        # a leaked session show up immediately instead of at 3am.
        pool_size=5,
        max_overflow=5,
    )
    _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine, _factory = None, None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session with a transaction around it. Commits on success, rolls back
    on failure, closes either way."""
    if _factory is None:
        raise RuntimeError("init_engine() has not been called")
    async with _factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
