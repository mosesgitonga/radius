"""Test configuration and fixtures.

Tests run against a **real PostgreSQL with PostGIS**, not a stub or SQLite. The
radius system is built out of `ST_DWithin`, GiST indexes, and geography maths; a
fake would pass tests that the real thing fails, which is worse than no test.
`docker compose up postgres redis` is enough to run the suite.

Isolation works by transaction, not by rebuilding the schema. Each test gets a
connection with an open transaction, a session joined to it via savepoints, and a
rollback at the end. Migrations therefore run once per session, tests never see
each other's rows, and committing inside a test behaves as it does in production
while still being undone afterwards.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# Must happen before anything imports settings: real environment variables take
# precedence over .env, so this points the suite at the test database and a
# dedicated Redis index instead of the development ones.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://myradius:myradius@localhost:5432/myradius_test",
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["LOG_FORMAT"] = "console"
os.environ["LOG_ACCESS"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession  # noqa: E402

from app.core import db as db_module  # noqa: E402
from app.core import redis as redis_module  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

pytest_plugins: list[str] = []


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def apply_migrations(settings: Settings) -> Iterator[None]:
    """Bring the test database up to head, once per session.

    Requested by the `engine` fixture rather than autouse, so that tests which
    touch no database — redaction, config validation, cursor encoding — run
    without one.

    Deliberately a synchronous fixture: Alembic's async env calls `asyncio.run`,
    which cannot be nested inside a running event loop.

    Migrations are used rather than `create_all` so the suite exercises the same
    DDL that production will, including the extension setup in 0001 — a model
    that works under `create_all` but has no migration is a bug this catches.
    """
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(config, "head")
    yield


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(settings: Settings, apply_migrations: None) -> AsyncIterator[AsyncEngine]:
    engine = db_module.get_engine()
    try:
        async with engine.connect():
            pass
    except Exception as exc:  # pragma: no cover — environment problem, not a test failure
        pytest.exit(
            "Cannot reach the test database at "
            f"{settings.database_url.rsplit('@', 1)[-1]}: {exc}\n"
            "Start it with: docker compose up -d postgres redis",
            returncode=4,
        )
    yield engine
    await db_module.dispose_engine()


@pytest_asyncio.fixture(loop_scope="session")
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection wrapped in a transaction that is always rolled back."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            # Rolls back everything the test did, committed or not: the session
            # above only ever commits savepoints inside this outer transaction.
            await transaction.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """A session that can commit without escaping test isolation."""
    session = AsyncSession(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        yield session
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture(loop_scope="session")
async def redis_client(settings: Settings) -> AsyncIterator[Redis]:
    """Redis on a dedicated database index, flushed before each test.

    Flushed rather than key-prefixed so that a leftover rate-limit counter from
    one test cannot make another pass or fail. The index is separate from the
    development one precisely because this flushes.
    """
    client = redis_module.get_redis()
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover — environment problem
        pytest.exit(
            f"Cannot reach the test Redis: {exc}\n"
            "Start it with: docker compose up -d postgres redis",
            returncode=4,
        )
    await client.flushdb()
    yield client
    await client.flushdb()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
@pytest.fixture
def app(settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    """An application whose requests join the test's transaction.

    The override mirrors the real dependency, commit included, so endpoint code
    behaves identically — the commit lands on a savepoint and the outer
    transaction rollback undoes it.
    """
    application = create_app(settings)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        try:
            yield db_session
        except Exception:
            await db_session.rollback()
            raise
        else:
            await db_session.commit()

    application.dependency_overrides[db_module.get_session] = override_get_session
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture(loop_scope="session")
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client speaking ASGI directly, so no port is bound."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"User-Agent": "myradius-tests"},
    ) as client:
        yield client
