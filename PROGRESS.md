# MyRadius Backend — Progress

Backend for MyRadius, a hyperlocal community app for Kenya. See the build brief for
product context. This file tracks what each phase planned, built, and deferred.

---

## Phase 0 — Foundation

**Status:** complete, pending verification of the Docker-dependent checks

### Plan

No domain models land in this phase. Phase 0 builds the chassis every later module
bolts onto, so the cross-cutting rules in the brief (privacy, blocking, rate limits,
pagination, error shape) each have exactly one home before there is any feature to
apply them to.

**Project and tooling**

- `uv` + `pyproject.toml`, Python 3.12 pinned via `.python-version`.
- Package layout: `app/core` (config, db, security, errors, pagination, rate limiting,
  logging), `app/modules/<name>/{models,schemas,service,router}.py`, `app/workers`.
- Module packages are created empty with a docstring naming the phase that fills them,
  so the layout doubles as a map of the build.
- `ruff` (lint + format), `mypy` (strict on `app/`), `pytest` + `pytest-asyncio` + `httpx`.

**Configuration**

- `pydantic-settings` `Settings`, cached singleton, env-var driven, with a complete
  `.env.example`. Phase 0 scope only: app, database, redis, logging, rate limits, CORS,
  pagination. Radius/H3/k-anonymity settings land in Phase 1 with the engine that reads them.

**Database**

- Async SQLAlchemy 2.x engine + `async_sessionmaker`, `asyncpg` driver.
- `Base` declarative base plus mixins every later model reuses: UUID primary key,
  `created_at`/`updated_at`, and `deleted_at` soft delete (moderation needs evidence).
- Alembic configured for async engines and GeoAlchemy2 (its types must render in
  autogenerate, and its spatial indexes must not be dropped as "unknown").

**Migrations**

- `0001_enable_extensions`: `postgis` and `pg_trgm`. Done as a migration rather than
  leaning on the PostGIS image's initdb scripts, so a plain PostgreSQL 16 also works and
  so "migrations apply cleanly on an empty database" is literally true.

**Cross-cutting core**

- One error envelope `{"error": {"code", "message", "details"}}` for every failure path:
  app errors, validation errors, `HTTPException`, and unhandled exceptions.
- Request-ID middleware: honours inbound `X-Request-ID` or generates one, carries it in a
  contextvar, echoes it on the response, and stamps it on every log line.
- Structured JSON logging on the stdlib (no new dependency) with a redaction filter for
  phone numbers, OTPs, tokens, and coordinates — applied to both messages and structured
  fields, since the brief forbids coordinates reaching logs at all.
- Redis-backed rate limiter (atomic fixed window via Lua) with per-IP, per-user, and
  per-arbitrary-key scopes, exposed as a FastAPI dependency factory.
- Cursor pagination helper: opaque base64 keyset cursors and a `CursorPage[T]` response
  model, so every list endpoint in every later phase has the same shape for free.

**Endpoints**

- `GET /health` — liveness, no dependency checks.
- `GET /ready` — readiness, checks PostgreSQL and Redis, 503 when either is down.

**Workers**

- `arq` worker settings + Redis connection, with a no-op heartbeat task to prove the
  worker boots and the schedule fires. Real tasks (`cell_activity` refresh, story
  expiry, retention) arrive in the phases that need them.

**Testing**

- pytest against a real PostGIS database. Per-test isolation by binding the session to an
  outer transaction rolled back after each test (`join_transaction_mode="create_savepoint"`),
  so tests share one migrated database without leaking state.
- The app's session dependency is overridden so requests through the ASGI client join the
  test transaction.
- Redis isolated to a dedicated database index, flushed per test.
- `tests/factories.py`: synthetic data helpers, including Nyeri and Karatina coordinates
  the brief's radius tests will need.

**Local dev and CI**

- Docker Compose: `api`, `worker`, `postgres` (PostGIS 16), `redis`, with healthchecks and
  `depends_on: service_healthy`.
- Multi-stage Dockerfile: `uv`-based build, dev target with reload, prod target running
  Gunicorn with Uvicorn workers.
- GitHub Actions: ruff, mypy, pytest with PostGIS and Redis service containers, plus a
  job that proves migrations upgrade and downgrade cleanly on an empty database.

### Open questions

- **Feed anchoring (Phase 1, flagged in the brief).** Home location, current location, or
  both? Does not block Phase 0; needs an answer before the Phase 1 location model.

### Built

**Project and tooling.** `uv` project on Python 3.12 (`.python-version`), ruff for lint
and format, mypy in `strict` mode over `app` and `tests` — both clean. 13 module packages
scaffolded under `app/modules`, each docstring naming the phase that fills it.

**Configuration.** `app/core/config.py`: env-driven `Settings` with a cached singleton and
a complete `.env.example`. Refuses to boot in staging or production with the development
signing key or `DEBUG=true`. Rejects a synchronous database URL at boot rather than at
first query.

**Database.** Async engine with `pool_pre_ping` and a server-side statement timeout; a
session dependency that is a unit of work (commit on return, rollback on raise). `Base`
plus the three mixins every later model reuses: UUID primary key, `timestamptz`
created/updated, and `deleted_at` soft delete. Constraint naming convention set, without
which Alembic cannot match up or drop constraints later.

**Migrations.** Alembic configured for async engines with GeoAlchemy2's helpers installed,
so spatial columns and their PostGIS-managed indexes autogenerate correctly. Sequential
four-digit revision IDs so migration files sort in apply order. Migration `0001` enables
`postgis` and `pg_trgm`. Generated migrations are formatted and linted by a post-write hook.

**Cross-cutting core.** One error envelope across app errors, validation errors,
`HTTPException`, and unhandled exceptions, with `X-Request-ID` attached even on the 500
path. Pure-ASGI request-context middleware (request ID, client IP, one access line per
request). Structured JSON logging on the stdlib with root-logger redaction. Redis client.
Atomic Lua-based rate limiter with per-IP, per-user, and per-key scopes, exposed as a
dependency factory. Keyset cursor pagination with a `CursorPage[T]` response model.

**Endpoints.** `GET /health` (liveness, no dependency checks) and `GET /ready` (readiness,
checks PostgreSQL and Redis concurrently, 503 with per-dependency results). Both documented
with examples; `/v1` router mounted and ready for Phase 1.

**Workers.** arq worker settings, a `with_job_context` decorator that adopts the job ID as
the request ID, and a heartbeat on the fifteen-minute cadence `cell_activity` will use.

**Testing.** 176 tests. Real PostGIS, per-test transaction rollback isolation, dependency
override so requests through the ASGI client join the test transaction, Redis on a
dedicated flushed index. `tests/factories.py` builds coordinates by kilometre offsets from
Nyeri, Karatina, Nanyuki, Othaya, and DeKUT anchors, checked against PostGIS.
`tests/test_privacy_schemas.py` scans response schemas for location leaks and includes a
self-test proving it can fail.

**Local dev and CI.** Compose stack (`api`, `worker`, `postgres`, `redis`) with healthchecks
and ordered startup; an init script that creates the test database. Multi-stage Dockerfile
with a shared dependency layer, a dev target with reload, and an unprivileged production
target running Gunicorn with Uvicorn workers. GitHub Actions with four jobs: lint and
types, tests, a migration upgrade/downgrade/upgrade cycle on an empty database, and a
production image build.

**Docs.** `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` (21 decisions with alternatives), and
`docs/DATA_MAP.md`.

### Verification status

Verified on this machine: ruff clean, mypy clean over `app` and `tests`, the app boots and
serves `/health`, `/ready`, `/docs`, and `/openapi.json`, the error envelope and request-ID
header behave as specified, `alembic upgrade head --sql` renders migration 0001 correctly
offline, both shell scripts pass `bash -n`, and **91 of 176 tests pass** — every test that
does not need a database.

**Not yet verified:** `docker compose up`, and the 85 tests that need PostGIS or Redis.
Docker Desktop was not installed on this machine during Phase 0; it is being installed
separately. Everything needed to run them is committed, and this section will be updated
once the stack has actually been brought up.

### Deferred

Deliberately not built, with the phase that will own each. Fuller reasoning in
`docs/DECISIONS.md`.

- **`Idempotency-Key` handling** — an API convention in the brief, but there are no create
  endpoints yet to retry. Lands in Phase 2 with the first content `POST`.
- **Authentication, and `request.state.user_id`** — Phase 1. The rate limiter already reads
  it and falls back to the client IP.
- **Radius, H3, and k-anonymity settings** — Phase 1, with the engine that reads them.
- **`ETag` support** — worthwhile only on resources that rarely change; none exist yet.
  Phase 1's area list and Phase 4's group metadata are the first candidates.
- **Model factories** — Phase 1, with the first models. Phase 0 ships the location and
  phone-number helpers the radius tests will need.
- **Production deployment notes, backups, metrics, error tracking** — Phase 8, per the brief.

### How to try it

```bash
cp .env.example .env
docker compose up --build
```

- <http://localhost:8000/docs> — interactive API docs
- <http://localhost:8000/health> — liveness
- <http://localhost:8000/ready> — readiness, with per-dependency results

To see the error envelope and a request ID:

```bash
curl -i http://localhost:8000/v1/nope
```

To watch readiness fail honestly, stop Redis and ask again:

```bash
docker compose stop redis
curl -s http://localhost:8000/ready    # 503, checks.redis false
curl -s http://localhost:8000/health   # still 200
docker compose start redis
```

The checks, as CI runs them:

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests
uv run pytest                 # needs postgres and redis up
```

`docker compose up postgres redis` is enough for the test suite; the API and worker
containers are not required.
