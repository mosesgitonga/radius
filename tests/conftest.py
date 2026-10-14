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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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
# At least 32 bytes, matching what production requires: a shorter key makes
# PyJWT warn, and a suite that runs with warnings it ignores teaches nobody
# anything.
os.environ["SECRET_KEY"] = "test-secret-key-at-least-32-bytes-long-for-hs256"
# Never real storage or SMS, whatever the developer's .env says: tests must not
# reach the network, and must not write into a bucket somebody is using.
os.environ["STORAGE_PROVIDER"] = "memory"
os.environ["SMS_PROVIDER"] = "console"

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession  # noqa: E402

from app.core import db as db_module  # noqa: E402
from app.core import redis as redis_module  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.core.providers.sms import ConsoleSmsProvider, set_sms_provider  # noqa: E402
from app.core.providers.storage import MemoryStorageProvider, set_storage_provider  # noqa: E402
from app.core.queue import RecordingJobQueue, set_queue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.modules.auth import service as auth_service  # noqa: E402
from app.modules.users.models import User  # noqa: E402
from tests.factories import NYERI_TOWN, make_user, offset_km  # noqa: E402

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


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def clean_database(engine: AsyncEngine) -> None:
    """Remove any committed rows before the session starts.

    Tests isolate themselves by transaction rollback, which cannot undo data
    somebody committed outside a test — running `scripts/seed_areas.py` against
    the test database, for instance, leaves fourteen areas behind that then show
    up in every area listing. CI always starts clean, so this would be a failure
    only a developer sees, which is the worst kind.

    The table list comes from the database rather than from `Base.metadata`,
    deliberately. Importing the test suite registers throwaway models on the
    shared declarative base — `tests/test_pagination.py` has one — and those
    tables are created inside a transaction and never exist on disk, so
    truncating a list built from metadata fails on a table that is not there.
    Asking PostgreSQL what exists is both simpler and catches tables metadata
    does not know about.

    Truncating once per session rather than per test keeps the rollback-based
    isolation that makes the suite fast.
    """
    async with engine.begin() as connection:
        rows = await connection.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' "
                # PostGIS owns spatial_ref_sys, and Alembic owns its version row.
                "AND tablename NOT IN ('spatial_ref_sys', 'alembic_version')"
            )
        )
        tables = [row[0] for row in rows]
        if not tables:
            return
        joined = ", ".join(f'"{table}"' for table in tables)
        # One statement, foreign keys included, and sequences reset so IDs do not
        # drift between runs.
        await connection.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))


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
@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def redis_client(settings: Settings) -> AsyncIterator[Redis]:
    """Redis on a dedicated database index, flushed before and after each test.

    Flushed rather than key-prefixed so that a leftover rate-limit counter or
    pending OTP from one test cannot make another pass or fail. The index is
    separate from the development one precisely because this flushes.

    **Autouse**, which matters more than it looks. Rate limits and OTP state live
    only in Redis, so a test that never mentions Redis still depends on it being
    clean — an OTP test running after twenty others would otherwise trip a daily
    cap that somebody else filled. Isolation that applies only when a test
    remembers to ask for it is not isolation.
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
def _install_session_override(application: FastAPI, db_session: AsyncSession) -> None:
    """Route the application's database dependency to the test's session.

    The override mirrors the real dependency — commit on success, rollback on
    failure — scoped so that a failing request undoes only its own work, exactly
    as a separate production request would.
    """

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        # Commit whatever the test set up before this request, because in
        # production anything that exists before a request arrives is already
        # committed. Without this, every request in a test shares one uncommitted
        # transaction, and a request that fails — a 404, say — rolls back the
        # test's own setup with it: the user, their session row, the post under
        # test. The next request then fails for reasons that have nothing to do
        # with what is being tested. Committing here only releases a savepoint;
        # the outer transaction still rolls everything back at the end.
        await db_session.commit()

        # The request's own work gets a savepoint, so a failure undoes exactly
        # that request and nothing before it — the production behaviour, where
        # each request has its own session.
        request_scope = await db_session.begin_nested()
        try:
            yield db_session
        except Exception:
            if request_scope.is_active:
                await request_scope.rollback()
            db_module.discard_after_commit(db_session)
            raise
        else:
            if request_scope.is_active:
                await request_scope.commit()
            await db_session.commit()
            # As in production: realtime events and pushes fire only once the
            # request's writes are committed.
            await db_module.run_after_commit(db_session)

    application.dependency_overrides[db_module.get_session] = override_get_session


@pytest.fixture(scope="session")
def shared_app(settings: Settings) -> FastAPI:
    """One application for the whole session.

    Building the app is not free: FastAPI constructs a pydantic adapter for every
    route parameter and response model at include time, about 150 ms for this
    API. Built per test, that was over a minute and a half of a four-and-a-half
    minute run, spent rebuilding an identical object 643 times.
    """
    return create_app(settings)


@pytest.fixture
def app(shared_app: FastAPI, db_session: AsyncSession) -> Iterator[FastAPI]:
    """The shared application, wired to this test's transaction.

    Do not add routes to it: they would persist into every later test. A test that
    needs to mount its own routes takes `fresh_app` instead.
    """
    _install_session_override(shared_app, db_session)
    yield shared_app
    shared_app.dependency_overrides.clear()


@pytest.fixture
def fresh_app(settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    """A private application, for tests that mount routes of their own.

    The error-envelope tests mount failing routes, and the privacy tests mount
    deliberately leaky ones to prove the scanner catches them. On the shared app
    those would leak — a leaky route left behind would make the real privacy scan
    fail in whichever test happened to run next.
    """
    application = create_app(settings)
    _install_session_override(application, db_session)
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


# ---------------------------------------------------------------------------
# Phase 1: providers, users, and authenticated clients
# ---------------------------------------------------------------------------
@pytest.fixture
def sms(settings: Settings) -> Iterator[ConsoleSmsProvider]:
    """The console SMS provider, installed for the duration of a test.

    A recording fake rather than a mock: tests assert on what was actually sent,
    and nothing in the suite can reach Africa's Talking even by accident.
    """
    provider = ConsoleSmsProvider()
    set_sms_provider(provider)
    yield provider
    set_sms_provider(None)


@pytest_asyncio.fixture(loop_scope="session")
async def user(db_session: AsyncSession) -> User:
    """An ordinary active user with a home location in Nyeri."""
    return await make_user(db_session, at=NYERI_TOWN)


@pytest_asyncio.fixture(loop_scope="session")
async def other_user(db_session: AsyncSession) -> User:
    """A second user, a couple of kilometres from the first."""
    return await make_user(db_session, at=offset_km(NYERI_TOWN, north=2.0))


@pytest_asyncio.fixture(loop_scope="session")
async def tokens(
    db_session: AsyncSession, settings: Settings, user: User
) -> auth_service.TokenPair:
    """A real token pair for `user`, issued through the service.

    Issued by the production code path rather than hand-signed, so the tests
    exercise the same session creation a sign-in does — including the session row
    that `get_current_user` checks on every request.
    """
    pair = await auth_service.issue_tokens(
        db_session, user=user, settings=settings, device_name="Test Device"
    )
    await db_session.flush()
    return pair


@pytest_asyncio.fixture(loop_scope="session")
async def auth_client(
    client: httpx.AsyncClient, tokens: auth_service.TokenPair
) -> httpx.AsyncClient:
    """The HTTP client with `user`'s access token attached."""
    client.headers["Authorization"] = f"Bearer {tokens.access_token}"
    return client


@pytest_asyncio.fixture(loop_scope="session")
async def authenticate(
    client: httpx.AsyncClient, db_session: AsyncSession, settings: Settings
) -> Callable[[User], Awaitable[httpx.AsyncClient]]:
    """Sign in as any user, for tests that need a second identity.

    Returns the same client with the header swapped: one client, one transaction,
    so both identities see the same database state.
    """

    async def _authenticate(as_user: User) -> httpx.AsyncClient:
        # A failed request rolls back its savepoint, and SQLAlchemy expires any
        # object that request loaded — including this user, if the request
        # authenticated as them. Reading an expired attribute then triggers a
        # synchronous reload, which async SQLAlchemy cannot do (MissingGreenlet).
        # Refreshing here, asynchronously, makes switching identity after an
        # error response safe.
        await db_session.refresh(as_user)
        pair = await auth_service.issue_tokens(db_session, user=as_user, settings=settings)
        await db_session.flush()
        client.headers["Authorization"] = f"Bearer {pair.access_token}"
        return client

    return _authenticate


# ---------------------------------------------------------------------------
# Phase 2: storage and the job queue
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def storage() -> Iterator[MemoryStorageProvider]:
    """A fresh in-memory object store for every test.

    Autouse for the same reason Redis isolation is: a test that never mentions
    storage can still be affected by an object another test left behind, and the
    process-wide provider singleton would otherwise carry them across tests.
    """
    provider = MemoryStorageProvider()
    set_storage_provider(provider)
    yield provider
    set_storage_provider(None)


@pytest.fixture(autouse=True)
def queue() -> Iterator[RecordingJobQueue]:
    """Records enqueued jobs instead of sending them to Redis.

    Tests assert that a job was queued, then call the worker task directly — as
    the brief asks — rather than standing up a worker mid-test.
    """
    recorder = RecordingJobQueue()
    set_queue(recorder)
    yield recorder
    set_queue(None)
