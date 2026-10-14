# MyRadius Backend — Progress

Backend for MyRadius, a hyperlocal community app for Kenya. See the build brief for
product context. This file tracks what each phase planned, built, and deferred.

---

## Phase 0 — Foundation

**Status:** complete and verified against the real stack on 2026-10-12

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

- ~~**Feed anchoring (Phase 1, flagged in the brief).**~~ **Answered:** anchored to home,
  with "explore here" as a session-only mode. Five supporting rules came with the answer
  (authored content always carries the author's home area; the explored location lives in
  Redis for ~2 hours and never in PostgreSQL; visitors are labelled with their home area;
  home changes are limited to ~once per 14 days; the verified badge stores only a daily
  yes/no for "was in home area today"). Recorded in full, with its implementation
  consequences and three open sub-questions, as decision 1.1 in `docs/DECISIONS.md`.

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

Verified in full once Docker was available.

| Check | Result |
| --- | --- |
| `ruff check` and `ruff format --check` | clean |
| `mypy app tests` (strict) | clean, 44 files |
| `pytest` | **176 passed**, twice in succession, 90% coverage |
| Migrations on an empty database | `upgrade head` → `downgrade base` → `upgrade head`, then `alembic check` clean |
| Extensions | absent before 0001, `postgis` + `pg_trgm` after, gone after downgrade |
| `docker compose up` | all four services healthy; API applies migrations on start |
| `GET /health`, `/ready`, `/docs`, `/openapi.json` | 200, with `X-Request-ID` on every response |
| Readiness under failure | Redis stopped → `/ready` 503 with `checks.redis: false`; `/health` stays 200 |
| Worker | boots, cron fired on the quarter hour, job ID carried into the logs |
| Production image | builds, runs as uid 1001, Gunicorn boots Uvicorn workers, container healthcheck healthy, JSON access logs |

Three defects were found by running it, all fixed:

1. **Every database test failed** with asyncpg's "attached to a different loop".
   `asyncio_default_fixture_loop_scope` was set but
   `asyncio_default_test_loop_scope` was not, so the session-scoped engine and
   the tests were on different event loops. Both are now set, with a comment
   saying why they must match.
2. **A wrong assertion** in `test_pages_cover_every_row_exactly_once`: label
   order *ascends* as recency descends, so `sorted(reverse=True)` was backwards.
   It now pins the exact expected sequence, which covers ordering and
   completeness together.
3. **The worker did not come back after a Redis bounce.** arq exits when it loses
   Redis, and compose had no restart policy, so the worker stayed dead while the
   API carried on looking healthy. From Phase 1 the worker owns the
   `cell_activity` refresh, so this would have meant every radius quietly going
   stale with nothing reporting it. All four services now set
   `restart: unless-stopped`; verified by bouncing Redis and watching the worker
   restart itself. Phase 8 should add alerting on arq's health-check key, so this
   is noticed rather than merely recovered from.

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

---

## Phase 1 — Accounts, consent and location

**Status:** complete and verified on 2026-10-12

### Plan

The phase that makes the product's differentiator real. Three things here are load
bearing and get the most care: the radius engine, the privacy of location, and
refresh-token handling.

**Dependencies to add** (all named in the brief's stack): `h3` (h3-py v4),
`phonenumbers`, `PyJWT`. `httpx` moves from the dev group to the main one, because the
Africa's Talking provider calls their REST API directly rather than pulling in their
synchronous SDK.

**Providers behind interfaces**

- `SmsProvider`: `ConsoleSmsProvider` (dev and tests, logs the code), and
  `AfricasTalkingSmsProvider` over their REST API. Tests never reach the network.
- Selected by config, so switching is an env change.

**Models**

- `users.User` — phone (E.164, unique), display name, 18+ confirmation, phone-verified
  timestamp, status, home cells at H3 resolutions 9/8/7/6 plus centroid, home-set and
  home-changed timestamps, `last_active_at`, a 14-bit home-presence mask, and
  placeholders for the verified badge and rating.
- `users.Consent` — one row per grant or revocation, with document version and timestamp.
- `users.Block` — blocker, blocked, unique pair.
- `auth.DeviceSession` — hashed refresh token, family ID, device metadata, expiry,
  revocation reason, and a `replaced_by` link so a reused token can be traced to its family.
- `location.Area` — seeded polygons with a name, kind, parent, town and county.
- `location.CellActivity` — keyed by `(resolution, h3_cell)` with active and registered
  counts, so one table serves both the radius engine (res 8) and k-anonymity (res 8/7/6).

**Migration** `0002`, hand-reviewed: GiST indexes on every geography column, B-tree on
every H3 column, and the composite `(created_at, id)` indexes the paginator needs.

**Endpoints**

- `auth`: OTP request and verify, signup, refresh, logout, logout-all, list sessions.
- `users`: get and update my profile, public profile, consents, blocks, deletion request
  and cancellation, data export.
- `location`: set home location, start and end explore mode, my radius, area lookup.

**The radius engine**

Per res-8 cell, not per user. Expand H3 rings from the viewer's cell, accumulating active
users until the feature's target is met, convert ring distance to kilometres from real H3
cell distances, clamp to the feature's bounds. Cached in Redis per `(feature, cell)` with
20% hysteresis so feeds do not jump between refreshes. `GET /v1/me/radius` rounds to
0.5 km.

**Privacy**

Raw coordinates are converted to an H3 res-9 cell on arrival and discarded. Area labels
snap up to the finest parent cell containing at least K registered users (K=10). Distances
are exposed as buckets only. The privacy schema scanner from Phase 0 guards all of it.

**Workers**

- `refresh_cell_activity` every 15 minutes, taking the heartbeat's slot.
- `roll_home_presence` daily, writing one bit per user per day.
- `purge_deleted_accounts` daily, honouring the 30-day grace period.

**Decided ahead of the phase** (recommendations accepted with "continue"):

- Exploring users do **not** count toward `cell_activity` density. Density reflects
  residents, or a crowd at a market shrinks every local's radius for the afternoon.
- Changing home **resets** the badge's 14-day consistency window, with vouching as the
  fast path back.
- Explore days are **excluded** from the badge count, neither a yes nor a no.

### Built

**Authentication.** Phone OTP (6 digits, HMAC-hashed in Redis, 5-minute expiry, 5
attempts, 60-second resend cooldown, daily caps per number and per IP, all failing
closed). JWT access tokens (15 minutes) and rotating refresh tokens (30 days)
stored as keyed hashes per device session, with reuse detection that revokes the
whole token family. Logout, logout-everywhere (keeping the current device by
default), and a sessions list. Signup takes a display name, an 18+ confirmation,
and explicit consents.

**Users.** Own profile and a public profile carrying an area label and a distance
band. Append-only consent records with document versions. Blocking, through the
one shared `blocked_user_ids` check every later module will use. Data export as
JSON and a deletion request with a 30-day grace period.

**Location.** `cells.py`: coordinates to H3 cells and nothing back. Home location
with a change cooldown, area resolution, and spoof flagging. Area labels with
k-anonymity, snapping up through res 8, 7, 6 and falling back to a generic label.
Distance buckets. Explore mode in Redis only. `GET /v1/me/radius`.

**The radius engine.** Per res-8 cell, ring expansion against precomputed density,
ring distances from real H3 geometry, 20% hysteresis, feed-only computation with
the other four features derived as multiples, and user distance filters capped per
feature.

**Workers.** `refresh_density` every quarter hour (and at startup), `roll_presence`
and `run_retention` nightly at 00:20 and 00:40 UTC — after midnight in Nairobi.

**Areas.** `areas` table, `scripts/seed_areas.py`, and `seeds/areas/nyeri.geojson`
with 14 areas including DeKUT. Nothing about Nyeri is in application code.

**Providers.** `SmsProvider` with a recording console implementation and an
Africa's Talking implementation over their REST API. Tests cannot reach the
network.

**Schema.** Migrations 0002 (seven tables) and 0003, both reviewed by hand.

**Endpoints:** 18 under `/v1`, bringing the total to 20.

### Verified

| Check | Result |
| --- | --- |
| `pytest` | **487 passed**, twice in succession, **89% coverage** |
| `ruff` + `mypy` (strict, `app` + `tests` + `scripts`) | clean, 77 files |
| Migrations on an empty database | 3 up, 3 down, 3 up again, `alembic check` clean |
| Compose stack | all four services healthy; API migrates on start |
| Full client flow against the live API | OTP → signup → set location → radius → explore → export → rotate → replay-detection, all as specified |
| Area labels | "nearby" with one user in the cell; "Nyeri Town" once 40 neighbours satisfy K=10 |
| Radius behaviour | empty 15.00 km, very sparse 11.95, rural 6.69, small town 3.19, dense estate 1.49, very dense 1.00 (floor) |
| Hysteresis | unchanged through a 10% density rise, moves on a sixfold one |
| Refresh reuse | replaying a consumed token revokes the family; the rotated token dies with it |

### Six defects found by testing, all fixed

1. **Refresh reuse detection did not persist.** The family revocation was rolled
   back by the exception that reported it, so the replayed token was refused while
   every other token in the family kept working — precisely the failure the
   mechanism exists to prevent.
2. **Account purging could never succeed.** `purge_user` wrote a 43-character
   placeholder into a `VARCHAR(20)` column, so the nightly retention job failed on
   its first real account. A Data Protection Act failure whose only symptom was a
   worker log line. A purged account now holds no phone number at all.
3. **No minimum secret-key length.** PyJWT warns that an HMAC key shorter than the
   hash output weakens the signature, then signs anyway. Production now refuses to
   boot below 32 bytes.
4. **Reserved log keys crashed logging, twice.** `extra={"created": ...}` and
   `extra={"message": ...}` both raise `KeyError` before any filter runs.
   `safe_extra()` corrects dynamic field names; the static ones were renamed.
5. **Redis errors were caught too narrowly.** A raw `OSError` escaping redis-py
   would have broken a radius computation that should merely have missed its cache.
6. **`:param::type` in raw SQL.** SQLAlchemy's `text()` parser chokes on it; the
   presence-roll statement failed outright until rewritten with `CAST`.

Two test-infrastructure fixes mattered beyond the suite: Redis isolation is now
autouse (rate limits live only in Redis, so a test that never mentions Redis still
depends on it being clean), and committed rows are truncated at session start
(transaction rollback cannot undo what the area seeder committed).

### Deferred

With the phase that will own each; reasoning in `docs/DECISIONS.md`.

- **Avatars** — Phase 2. The column exists and stays null until the media table does.
- **Content reach estimates** — Phase 2. The density data is already in `cell_activity`.
- **Badge computation** — Phase 8. Phase 1 records the presence bits it will read.
- **Moderation queue, moderator roles, restrictions, ban lists** — Phase 8.
- **Vouching** — Phase 8, as the fast path back after a home change resets the window.
- **`Idempotency-Key`** — Phase 2, with the first create endpoint worth retrying.
- **`ETag`** — Phase 4. The areas list is the first cheap candidate.

### How to try it

```bash
cp .env.example .env
docker compose up --build
uv run python -m scripts.seed_areas seeds/areas/nyeri.geojson
```

Then, with `SMS_PROVIDER=console`, the code is printed in the API log:

```bash
curl -X POST localhost:8000/v1/auth/otp/request \
  -H 'Content-Type: application/json' -d '{"phone_number":"0712345678"}'

docker compose logs api | grep 'code is'
```

Verify it, sign up, and the response carries a token pair. Then:

```bash
# Coordinates go in...
curl -X PUT localhost:8000/v1/me/location -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"latitude":-0.4201,"longitude":36.9476}'

# ...an area label comes back, and never a coordinate.
curl localhost:8000/v1/me/radius -H "Authorization: Bearer $TOKEN"
```

A single user is below K=10, so the label is `nearby`. To see a real one, create
some synthetic neighbours and run the density refresh — `tests/factories.py` has
the helpers, and `tests/test_radius.py` shows the pattern.

Interactive docs, with examples, at <http://localhost:8000/docs>.

### Open questions

_None blocking. Three sub-questions raised during planning were answered with the
recommended values and are recorded as decision 1.1 in `docs/DECISIONS.md`._


---

## Phase 2 — Media and neighbourhood feed

**Status:** complete and verified on 2026-10-14

### Plan

The first phase where users create content, which makes three things real that were
only scaffolding before: the radius actually filters something, blocking actually
hides something, and moderation has something to moderate.

**Dependencies to add** (both named in the brief's stack): `Pillow` for image
processing and `boto3` for S3-compatible presigning. boto3 is synchronous, which is
fine for presigning — that is a local HMAC computation, not a network call — and
the worker's object transfers go through `asyncio.to_thread`.

**Storage**

- `StorageProvider` protocol with two implementations: `S3StorageProvider` (R2, S3,
  MinIO — all the same API) and `MemoryStorageProvider` for tests, so the suite
  never touches the network.
- MinIO added to `docker-compose` for development, so the presigned-upload flow is
  exercised for real locally rather than only in production.

**Media pipeline**

1. `POST /v1/media/uploads` returns a presigned PUT and a media row in `pending`.
2. The client uploads straight to storage — the API never proxies image bytes.
3. `POST /v1/media/{id}/complete` enqueues processing.
4. The worker validates the real content type from magic bytes (not the client's
   claim), **strips all EXIF** including GPS, generates WebP at 320/720/1280, and
   **deletes the original**.
5. Only processed variants are ever served.

**Models**

- `media.MediaAsset` — owner, purpose, status, content type, dimensions, variant
  keys, and the original's key until it is discarded.
- `feed.Post` — author, body, category, its own H3 cells and centroid, status,
  cached reach estimate and counters, plus a nullable `group_id` for Phase 4.
- `feed.PostImage` — ordered, up to 6 per post.
- `feed.Story` — image or text, 24-hour expiry, with `StoryView` for the author's
  viewer list.
- `feed.Comment` — one level of replies, enforced by a check on `parent_id`.
- `feed.Reaction` — one per user per target, on a post or a comment.
- `moderation.Report` — reporter, target, reason, status, priority.
- `core.IdempotencyRecord` — the `Idempotency-Key` support deferred from Phase 0.

**Feed query**

`ST_DWithin` against the viewer's radius, newest first, keyset-paginated, excluding
blocked users and hidden content. Shaped so that ranking can be added later without
changing the response: the endpoint takes a `sort` parameter with one value today.

**Reach estimate**

Returned on post creation: the active users in the cells whose own radius covers the
post's location. Approximate and cached, as the brief allows.

**Moderation**

Reports from distinct *trusted* reporters auto-hide content pending review. Trust in
Phase 2 is account age plus phone verification plus no upheld reports against them;
Phase 8 swaps in the verified badge.

**Rate limits**

Tighter for new accounts: a configurable age threshold with separate posting,
commenting and story limits either side of it.

**Workers**

- `process_media` — the pipeline above.
- `expire_stories` — hourly cleanup.

### Built

**Media.** Presigned direct-to-storage uploads; the API never handles image bytes.
A worker validates from magic bytes, re-encodes to WebP at 320/720/1280 px from
decoded pixels (so no EXIF can survive), and deletes the original. A five-minute
catch-up sweep processes uploads whose job was lost, and abandoned uploads are
swept after a day. Avatars, as a real foreign key.

**Feed.** Posts with up to six images and a configurable category, placed at the
author's home cell. `GET /v1/feed` within the adaptive radius, newest first,
keyset-paginated, excluding blocked users and hidden or removed content, with a
`sort` field ready for ranking. Reach estimate on creation. Explore mode browses
another area; posting still goes home.

**Comments** with one level of replies, visitor marking, and post-author deletion.
**Reactions** on posts and comments, one per person, from a configurable list.
**Stories**, text or image, 24 hours, with an author-only viewer list; expired
stories are deleted with their images unless a report is pending.

**Reporting** for posts, comments, stories and users, prioritised so job scams and
safety come first, with auto-hide after three distinct trusted reports. People are
never auto-hidden.

**Cross-cutting.** `Idempotency-Key` on post, comment and story creation.
New-account rate limits. The purge and export now reach content and images.

**Infrastructure.** SeaweedFS in compose for local S3; separate presign endpoint;
`StorageProvider` with S3 and in-memory implementations; a job-queue seam.

**Schema.** Migrations 0004 (nine tables) and 0005, reviewed by hand.

**Endpoints:** 26 new, 46 in total across 36 paths.

### Verified

| Check | Result |
| --- | --- |
| `pytest` | **646 passed** in 78 s, **85% coverage** |
| `ruff` + `mypy` (strict, 99 files) | clean |
| Migrations on an empty database | 5 up, 5 down, 5 up, `alembic check` clean |
| Privacy scan | 49 response schemas, no location fields |
| OpenAPI | every endpoint has a summary and a description |
| Live stack, real S3 | presigned PUT to SeaweedFS → real worker container → variants downloaded |

**The brief's acceptance criteria, against the live stack:**

| Criterion | Result |
| --- | --- |
| Two users in the same area see each other's posts | Kamau, 1 km from Wanjiku, sees her post (band `under_1km`) |
| A user far away doesn't | A Nairobi account does not |
| Blocked users' content never appears | After Kamau blocks Wanjiku: gone from his feed and stories; 404 by ID |
| Uploaded images come back without EXIF | A photo with four GPS tags in → every downloaded variant has no EXIF and no GPS; **no original left in the bucket** |

### Defects found, all fixed

1. **HEIC uploads were accepted and then failed.** Presign accepted HEIC, the user
   paid to upload it, and processing rejected it as "could not be read" — Pillow
   cannot decode HEIC without a plugin. Now refused before upload. See open question.
2. **Import cycle.** `users.service` imported the audit writer from
   `moderation.service`, which reports then needed to import back. The audit writer
   is now a leaf module.
3. **Feed order depended on how data was loaded.** PostgreSQL's `now()` is the
   transaction's start time, so rows created together shared a timestamp and fell
   back to random-UUID order. `created_at` now has a wall-clock Python default.
4. **CRLF broke the container.** `entrypoint.sh` picked up Windows line endings
   from a script I edited, and the API restart-looped on `set: pipefail: invalid
   option name`. `.gitattributes` governs checkout, not files written directly; the
   tree is normalised and a test now fails on CRLF in shell scripts.
5. **MinIO is gone from Docker Hub.** Replaced with SeaweedFS, which needed a
   separate presign endpoint (the host is part of a SigV4 signature) and a
   healthcheck on `127.0.0.1` (busybox resolves `localhost` to IPv6 first).
6. **Two test-harness isolation bugs.** A failed request rolled back the test's own
   setup, because all requests in a test shared one uncommitted transaction; and its
   rollback expired objects later read synchronously. Each request now gets a
   savepoint, after setup is committed.
7. **The suite slowed from 31 s to 4½ minutes.** `create_app()` costs 150 ms and ran
   per test. Built once per session now, with private apps for the two kinds of test
   that mount routes. 52 s on the same tests.

### Deferred

With the phase that will own each; reasoning in `docs/DECISIONS.md`.

- **Feed ranking** — the field and query shape are ready.
- **Notifications** for comments, replies and reactions — Phase 3.
- **The moderation queue and moderator actions** — Phase 8. Reports are filed,
  prioritised, and auto-hide works; nothing can resolve one yet.
- **Hard purge of soft-deleted content** — Phase 8's retention job.
- **A CDN** — supported via `STORAGE_PUBLIC_BASE_URL`, not yet configured.
- **HEIC** — pending a decision on `pillow-heif`.

### How to try it

```bash
cp .env.example .env        # now includes the SeaweedFS storage settings
docker compose up --build   # five services: api, worker, postgres, redis, storage
```

The upload flow, as the app performs it:

```bash
# 1. Ask for an upload URL
curl -X POST localhost:8000/v1/media/uploads -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"purpose":"post","content_type":"image/jpeg"}'

# 2. PUT the file to upload_url, with exactly the upload_headers returned
curl -X PUT "$UPLOAD_URL" -H 'Content-Type: image/jpeg' --data-binary @photo.jpg

# 3. Confirm, then poll until ready
curl -X POST localhost:8000/v1/media/$MEDIA_ID/complete -H "Authorization: Bearer $TOKEN"
curl localhost:8000/v1/media/$MEDIA_ID -H "Authorization: Bearer $TOKEN"

# 4. Post it; send the same Idempotency-Key twice to see the replay
curl -X POST localhost:8000/v1/posts -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' \
  -d '{"body":"Mount Kenya clear today","category":"general","media_ids":["'$MEDIA_ID'"]}'

curl localhost:8000/v1/feed -H "Authorization: Bearer $TOKEN"
```

Download any variant URL and inspect it: there is no EXIF.

### Open questions

- **HEIC support.** iPhones save photos as HEIC by default. Pillow cannot decode it
  without the `pillow-heif` package, which is not in the agreed stack. Options: add
  `pillow-heif` (a small, well-maintained Pillow plugin) so HEIC is accepted and
  converted server-side; or keep refusing it and have the Flutter app convert to
  JPEG before upload. Android is unaffected either way.

