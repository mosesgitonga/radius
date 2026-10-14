"""Database engine and session management.

One lazily built async engine per process, shared by the API and the worker. The
engine is created on first use rather than at import time so that importing any
module — in tests, in Alembic, in a one-off script — does not open sockets.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql import Executable

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _build_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict[str, object] = {}
    if settings.db_statement_timeout_ms > 0:
        # Enforced by the server, so it also covers a query whose client has gone
        # away. One slow PostGIS scan must not hold a pooled connection forever.
        connect_args["server_settings"] = {
            "statement_timeout": str(settings.db_statement_timeout_ms),
            "application_name": settings.app_name,
        }

    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        # Recycle before a proxy or the server drops an idle connection, and check
        # liveness on checkout; a stale connection otherwise surfaces as a random
        # failed request.
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine(get_settings())
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            # Objects stay usable after commit, so a handler can still read the
            # row it just wrote without a second query.
            expire_on_commit=False,
            # No implicit flush before every query. Explicit flushes make it
            # obvious where a write hits the database, which matters in async
            # code where an unexpected flush can interleave with other work.
            #
            # The cost is real and worth knowing: a pending `session.add()` is
            # **not** visible to a later `select()` in the same transaction until
            # something flushes. Service code that adds a row and then queries for
            # it must call `await session.flush()` in between.
            autoflush=False,
        )
    return _sessionmaker


async def dispose_engine() -> None:
    """Close the pool. Called on application shutdown and by test teardown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency giving one session, and one transaction, per request.

    The session commits when the handler returns and rolls back when it raises —
    a unit of work per request. Services may commit mid-request when they need
    to, but they do not have to remember to commit at the end, which is the
    failure mode that silently loses writes.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            discard_after_commit(session)
            raise
        else:
            await session.commit()
            await run_after_commit(session)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Session for code outside a request: worker tasks, scripts, startup."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            discard_after_commit(session)
            raise
        else:
            await session.commit()
            await run_after_commit(session)


async def check_database() -> bool:
    """Cheap readiness probe. Never raises; the caller reports the status."""
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        logger.warning("database_unavailable", exc_info=True)
        return False
    return True


async def execute_dml(
    session: AsyncSession,
    statement: Executable,
    params: Mapping[str, Any] | None = None,
) -> int:
    """Run an INSERT, UPDATE, or DELETE and return the rows it affected.

    SQLAlchemy types `execute()` as returning `Result`, but `rowcount` lives on
    `CursorResult`, which is what a DML statement actually produces. The cast is
    here, once, rather than as a type-ignore at every retention job that wants to
    log how much work it did.
    """
    result = await session.execute(statement, params)
    return cast("CursorResult[Any]", result).rowcount


# ---------------------------------------------------------------------------
# After-commit callbacks
# ---------------------------------------------------------------------------
AFTER_COMMIT_KEY = "after_commit_callbacks"

AfterCommitCallback = Callable[[], Awaitable[None]]


def after_commit(session: AsyncSession, callback: AfterCommitCallback) -> None:
    """Run `callback` once this session's transaction has committed.

    For anything that tells the outside world a write happened — publishing a
    realtime event, enqueueing a push — because doing it before the commit gets
    two things wrong. The recipient can be told about a message they then cannot
    read, since the row is not visible yet. And if the transaction rolls back, they
    were told about a message that never existed.

    Callbacks are discarded on rollback, run in the order registered, and a failure
    in one is logged rather than raised: by the time they run the request has
    already succeeded, and an unavailable Redis must not turn a sent message into
    an error the client would retry, sending it twice.
    """
    session.info.setdefault(AFTER_COMMIT_KEY, []).append(callback)


def discard_after_commit(session: AsyncSession) -> None:
    session.info.pop(AFTER_COMMIT_KEY, None)


async def run_after_commit(session: AsyncSession) -> None:
    callbacks: list[AfterCommitCallback] = session.info.pop(AFTER_COMMIT_KEY, [])
    for callback in callbacks:
        try:
            await callback()
        except Exception:
            logger.exception("after_commit_callback_failed")
