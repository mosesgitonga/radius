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

---

## Phase 3 — Messaging and notifications

**Status:** complete and verified on 2026-10-15

### Plan

The first phase with realtime delivery, and the first where one user can put
something directly in front of another — which makes message requests and blocking
the load-bearing rules, not the transport.

**Dependencies.** `firebase-admin` (named in the brief) for FCM. The tests also use
the `websockets` client, which is already installed as part of `uvicorn[standard]`;
it is declared in the dev group so the test suite does not rely on a transitive
dependency.

**Ordering: a per-conversation sequence, allocated under a row lock.** "Messages
since cursor" needs an order that matches commit order, and neither timestamps nor a
global sequence give that — a global sequence value is assigned at insert, so a
later number can commit first and a syncing client skips the earlier one. Allocating
`seq` by incrementing the conversation row holds its lock until commit, so within a
conversation, sequence order *is* commit order.

**Persist first, then publish — after commit.** A new `after_commit` hook in
`core/db.py` runs callbacks only once the request's transaction has committed, and
discards them on rollback. Realtime publishes and push jobs go through it, so a
recipient is never told about a message they cannot yet read, and a rolled-back
message is never announced.

**Models**

- `Conversation` — kind (direct, group, listing), title, a unique `direct_key` so a
  pair can only ever have one direct conversation, request state, `last_seq`, and a
  nullable `listing_id` for Phase 5 and `group_id` for Phase 4.
- `ConversationMember` — role (owner, admin, member), status (active, invited, left,
  removed), `last_read_seq`, muted.
- `Message` — sender, `seq`, body or image, reply-to, soft delete.
- `DeviceToken`, `NotificationPreference`, `Notification` (with a collapse key and a
  burst count).

**Message requests.** A direct message to someone with no accepted conversation
creates the conversation in a `pending` state. The sender may send one message; the
recipient can accept, decline, or block and report, and replying counts as accepting.
A declined request cannot be reopened by the sender. Being added to a group chat by
someone you have no accepted conversation with makes you `invited`, not a member.

**Realtime.** `/v1/ws`, authenticated with the access token in the `Authorization`
header, or in a first `auth` frame for clients that cannot set headers — never in the
query string, which reverse proxies log. Each API process runs one hub: it subscribes
to a Redis channel per locally connected user and fans out to their sockets, so
delivery works across any number of workers. Presence is a per-user sorted set of
connection expiries, refreshed by heartbeat.

**Sync.** `GET /v1/sync?since=` lists conversations changed since a cursor, with
their `last_seq`; the client then fetches messages after its local `seq` per
conversation. Overlap is harmless, because per-conversation sync is idempotent.

**Notifications.** Device registration, per-type preferences, an in-app list, and
push from the worker to users who are not connected. Bursts collapse: repeat events
with the same collapse key update one unread notification ("Kamau and 3 others
reacted"), and pushes per key are throttled. Message pushes show the sender, not the
text, unless the user opts in — phones are often shared.

**Blocking.** No direct conversation, request, or message between a blocked pair, in
either direction. In a shared group chat, each is filtered out of the other's
delivery and history.

### Built

**Conversations.** Direct conversations, exactly one per pair. Message requests, as
the brief describes:

- the first message lands in the recipient's requests;
- the sender can send nothing more until a reply or an explicit accept;
- the recipient can accept, decline, or block-and-report — which files a report
  against the message and then blocks the sender;
- a decline is indistinguishable, to the sender, from no answer.

**Group chats** with a configurable member limit:

- **Roles:** one owner, admins and members, with ownership handed over or passed on
  when the owner leaves.
- **Joining:** people the adder knows join at once; strangers are invited and see
  nothing until they accept.
- **History:** members see history only from when they joined.

Listing conversations exist as a kind, for Phase 5.

**Messages.** Text, images through the media pipeline (purpose `message`),
reply-to, and delete-for-everyone, which erases the content unless a report is
pending. Sequence numbers are allocated under the conversation's row lock, so
sequence order is commit order. Read state is one number per member. Sync works at
two levels:

- `GET /v1/sync?since=` lists the conversations that changed;
- `messages?after_seq=` catches up within one;
- `messages/changes` finds deletions.

**Realtime.** `/v1/ws`, authenticated by header or by a first frame, never by
query string. Each API process runs one Redis pub/sub hub. Events are doorbells
carrying IDs, published only after commit. Presence is a sorted set of
connections. A socket closes when its token expires — unless the client
re-authenticates in-band — and closes at once when its session is revoked, on
whichever worker holds it.

**Notifications.**

- **Devices:** FCM token registration, tied to the sign-in, so signing out stops
  pushes.
- **Preferences:** per type, with previews off by default.
- **In-app list:** collapses bursts into one entry, e.g. "Kamau and 3 others
  reacted"; reading an entry does not reorder the list.
- **Feed hooks:** comments, replies and reactions.
- **Push from the worker:** only to people who are offline, haven't muted the
  conversation and aren't blocked with the sender, throttled per collapse key.
  Dead tokens are forgotten.

`PushProvider` has a recording fake and FCM behind it.

**Cross-cutting.**

- `after_commit` hooks in the unit of work.
- `authenticate_access_token`, shared by HTTP and the socket.
- Tiered limits for messages and for message requests.
- `Idempotency-Key` on sending.
- Export, purge and the retention job reach messages, devices and notifications.
- Enum values are enforced by the database (0006).

**Schema.** Migrations 0006 (enum CHECK constraints for 14 existing columns), 0007
(six tables), and 0008 (member visibility, deletable content, device sessions,
message reports), each reviewed by hand.

**Endpoints:** 28 new, 74 in total across 61 paths, plus the socket.

### Verified

| Check | Result |
| --- | --- |
| `pytest` | **773 passed**, **91% coverage** (6 minutes with coverage on) |
| `ruff` + `mypy` (strict, 120 files incl. tests) | clean |
| Migrations | 0005 → 0008 down and up on the test database; `alembic check` clean; 0006–0008 applied on boot to the development database holding Phase 2 data |
| Privacy scan | 65 response schemas, 222 fields, no location fields |
| OpenAPI | every operation has a summary and a description — **now a test** |
| Live stack | real API and worker containers: request held, second message refused, reply rang the socket, offline push delivered by the worker |

**The brief's acceptance criteria**, from `tests/test_realtime_live.py`. That test
runs two separate `uvicorn` processes sharing the test database and Redis:

| Criterion | Result |
| --- | --- |
| Two clients exchange messages in realtime across two API workers | Alice on worker 1 (header auth) and Bob on worker 2 (first-frame auth) ring each other; Bob fetches the message through worker 2. Twelve concurrent sends split across both workers get sequence numbers 3–14, no gaps or repeats |
| Offline users get a push | Bob closes his socket and presence clears. The API queues `deliver_message_push` in Redis; running it pushes to Bob's phone — sender's name, no text |
| Message requests behave as described | Carol's first message reaches Bob's requests and notifies him live; her second is refused with 409 `awaiting_reply` |
| Blocked users cannot message each other | After Bob blocks Alice: sends both ways are 404, reopening is 404, and nothing reaches Alice's socket |

Also proved live:

- signing out on worker 2 closes the socket on worker 1 (4401 `session_revoked`);
- a token in the query string is ignored;
- an unauthenticated socket is closed.

**One unreproduced flake.** `test_a_token_in_the_query_string_is_ignored` failed
once in 15 runs of the live module. That run came straight after a six-minute
coverage run, with the machine under load, and its detail was not captured. It
passed the next 12 runs in a row. The likely cause is timing in the test's close
wait rather than the server accepting the token: the same rejection path is
covered by the bad-token and no-token tests, which never failed. If it recurs, the
assertion reports the close code it got.

### Defects found, all fixed

1. **No enum column was constrained, ever.** Decision 1.16 said enums were CHECK
   constraints; SQLAlchemy has not emitted them by default since 1.4, so every
   status, reason and kind column accepted any string. Found by a stray insert.
   Fixed: `db_enum`, migration 0006, and a test that every constraint matches its
   enum exactly.
2. **Autogenerate rendered every new enum constraint twice**, and the migration
   failed on the duplicate. The explicit copies were removed from 0007.
3. **Reuse detection would not have closed sockets.** It commits by hand (1.3),
   which bypasses the after-commit step. It now runs the callbacks itself; a test
   proves the event arrives.
4. **"Mark all read" reshuffled the notification list.** The update bumped every
   row's activity time. It now leaves `updated_at` alone.
5. **Rows updated together tied on `updated_at`** — the 2.19 defect again, on the
   column Phase 3 sorts by. `updated_at` now has a Python clock too.
6. **Coverage under-reported async code by half.** Without greenlet tracing, the
   messaging service measured 49% when it is 94%.
7. **Four endpoints had no description.** There is now a test for it.
8. **`dict()` over a SQLAlchemy `Result`** — a lint autofix — failed at runtime,
   because a `Result` has `.keys()`. Caught by the suite.
9. **Every write since Phase 0 committed after the response was sent.** FastAPI
   runs a `yield` dependency's exit code after sending, by default, so a failed
   commit could follow a 201. Invisible in-process; caught by the two-worker test,
   where a job queued at commit was sometimes missing when the client already had
   its answer. Every route now uses one function-scoped `SessionDep`, and a test
   walks the routes to keep it that way (decision 3.22).

### Deferred

With the phase that will own each; reasoning in `docs/DECISIONS.md`.

- **Listing conversations** — Phase 5. **A group's linked chat** — Phase 4.
- **Typing indicators, message reactions, editing** — not in the brief.
- **Swahili push text** — strings are English and rendered server-side.
- **Moderator view of reported messages** — Phase 8; the content is kept for it.
- **Hard purge of soft-deleted messages** — Phase 8's retention job.
- **FCM installation IDs** — firebase-admin 7.7 deprecates addressing by
  registration token, but moving needs a client change.
- **HEIC** — still open, below.

### How to try it

```bash
docker compose up --build   # migrations 0006-0008 run on boot
```

With two signed-in users (`$A`, `$B` are access tokens, `$B_ID` is B's user ID):

```bash
# A opens a conversation and sends a request
CID=$(curl -s -X POST localhost:8000/v1/conversations/direct -H "Authorization: Bearer $A"   -H 'Content-Type: application/json' -d '{"user_id":"'$B_ID'"}' | jq -r .id)
curl -X POST localhost:8000/v1/conversations/$CID/messages -H "Authorization: Bearer $A"   -H 'Content-Type: application/json' -d '{"body":"Is the bike still for sale?"}'

# B sees it in requests; a second message from A is refused with 409
curl "localhost:8000/v1/conversations?folder=requests" -H "Authorization: Bearer $B"

# A listens on the socket (websocat, or any client that can set a header)
websocat -H "Authorization: Bearer $A" ws://localhost:8000/v1/ws

# B replies, which accepts; A's socket prints a message.created event
curl -X POST localhost:8000/v1/conversations/$CID/messages -H "Authorization: Bearer $B"   -H 'Content-Type: application/json' -d '{"body":"Yes, come see it"}'
```

Pushes go to the fake provider in development. The worker log shows
`push_not_sent_fake_provider` for each one. For real pushes, set
`PUSH_PROVIDER=fcm` and mount a Firebase service-account file at
`FCM_CREDENTIALS_FILE` (`.gitignore` covers the usual names).

### Open questions

- **HEIC support**, carried from Phase 2. Either add `pillow-heif` so iPhone photos
  are converted on the server, or keep refusing HEIC and have the Flutter app send
  JPEG.
- **Development `SECRET_KEY`** (minor). The shipped development default is 29
  bytes, so PyJWT warns when signing locally. Production and staging already
  refuse anything under 32 bytes. Lengthening the default is a one-line change if
  you want the warning gone.

---

## Phase 4 — Groups

**Status:** complete and verified on 2026-10-15

### Plan

Groups are the first place where *other users* hold power over someone: an admin
can remove, ban or mute them, and approve or delete what they post. So the work
is mostly authorisation, and the brief's "done when" is all permission tests.

**Models**

- `Group`:
  - name, description, category from a configurable list (estate, church, chama,
    students, sports, business, other), and rules;
  - visibility (`public` or `private`), join policy (`open` or `approval`), and
    whether posts need approval;
  - an optional cover image (a new `group` media purpose), the owner, and a member
    count.
- **Location only for public groups.** A public group stores a cell and centroid,
  taken from its creator's home when it is made public, so it can be discovered.
  A private group stores no location at all: it never appears in discovery, so it
  has no use for one.
- `GroupMember`: role (`owner`, `admin`, `moderator`, `member`) and status
  (`active`, `pending` for a join request, `invited`, `banned`), plus `muted_until`
  and who invited them. A ban is a membership row with status `banned`, so "banned
  users can't rejoin" is a property of one row that every way in checks.
- `GroupInviteLink`: a hash of the token (the token itself is shown once, like a
  refresh token), an expiry, an optional use limit, a use count, and a revocation
  time.
- `posts` gains `approval_status` (null outside groups; `pending`, `approved` or
  `rejected` in a group that requires approval) and `pinned_at`. `posts.group_id`
  and `conversations.group_id` become real foreign keys.

**Permissions** — one table in code, `groups.permissions`, checked by every route:

| Action | Owner | Admin | Moderator | Member |
| --- | --- | --- | --- | --- |
| Post, comment, react (unless muted) | ✓ | ✓ | ✓ | ✓ |
| Approve or reject pending posts; delete posts; mute members | ✓ | ✓ | ✓ | |
| Pin posts; approve join requests; invite; manage invite links | ✓ | ✓ | | |
| Remove and ban members; set rules and settings; enable the chat | ✓ | ✓ | | |
| Make someone a moderator | ✓ | ✓ | | |
| Make someone an admin; transfer ownership; delete the group | ✓ | | | |

Nobody acts on someone of equal or higher rank: an admin cannot remove another
admin, and nobody can remove or ban the owner.

**Reading.**

- **A public group** and its posts are readable by anyone who is signed in.
  Posting, commenting and reacting need membership.
- **A private group** is readable only by its members. Someone invited, or holding
  a valid invite link, can see its name and description, never its posts.
- **A banned user** gets a 404 for the group, as for anything else they may not
  see.
- Group posts never appear in the neighbourhood feed (Phase 2 already enforces
  this).
- **Posts from someone blocked are filtered out** inside groups too, as is that
  person in member lists.

**Joining.** A public open group joins at once. A public approval group creates a
join request that admins approve or reject. A private group can be joined only by
a direct invite (accepted by the invitee) or a valid invite link. Every one of
these paths checks the ban first.

**Discovery.** `GET /v1/groups/discover` lists public groups within the groups
radius (twice the feed radius, 2–25 km), from home or from an explore session.
The user's distance filter goes up to 25 km. It searches by name with `pg_trgm`
(trigram similarity, so "kamakwa estate" finds "Kamakwa Estates Residents") and
filters by category. Each group is shown with its k-anonymous area label and a
distance band, never a cell.

**Group posts** reuse `feed.service.create_post`, gaining a group scope. Comments,
reactions and reports on group posts go through the existing endpoints:
`feed.service.get_post` asks a leaf module, `groups.access`, whether the viewer
may read the group. That module imports only models, so there is no import cycle.

**Linked chat.** An admin can enable a group chat — a Phase 3 group conversation
with `group_id` set. Its membership follows the group: joining adds you, and
leaving, removal or a ban takes you out. Its members cannot be managed from the
chat side.

**Notifications** (plain-string types, no migration): a group invite to the
invitee; a join request to the admins; join approved, and post approved, to the
requester or author.

**Rate limits:** tiered group creation (new accounts 1 a day, established 5), join
requests, and invites.

**Data rights.** Export lists the groups a person created and their memberships.
Purge takes them out of every group and passes on any group they owned (admin,
then moderator, then the longest-standing member). A group left with nobody is
deleted. Their group posts are already covered by the feed purge.

### Built

**Groups**:

- **Public groups** are placed at their creator's home and discoverable nearby.
- **Private groups** store no location and are never discovered.
- **Settings:** configurable categories, rules, a cover image, an open or approval
  join policy, and optional post approval.

**Discovery.** `GET /v1/groups/discover` searches within the groups radius (twice
the feed's), or the user's own filter up to 25 km, from home or explore mode. Name
search uses `pg_trgm` word similarity, so it tolerates typos. Results can be
filtered by category, and are placed by area label and distance band.

**Joining:**

- **Public groups:** an open group is joined at once; an approval group gets a join
  request that admins approve or reject.
- **Direct invitations**, which the invitee accepts or declines.
- **Invite links**, stored hashed: they always expire, can be limited in uses and
  revoked, and admit straight in.
- **Bans come first:** every route into a group checks for a ban before anything
  else.

**Roles and admin tools.** Owner, admin, moderator and member, from one permission
table, and nobody acts on an equal or higher rank. The tools:

- approve and reject join requests;
- remove, ban and unban members — a ban can be placed on someone not yet a member;
- mute until a time;
- appoint moderators and admins, and transfer ownership;
- edit settings and rules, including switching between public and private;
- approve, reject, pin (up to three) and remove posts;
- enable a linked chat;
- delete the group.

Consequential actions are audited.

**Group posts** reuse the post model. Comments, reactions and reports go through
the existing endpoints, which now ask `groups.access` whether the viewer may read
the post and take part. Moderators' own posts skip approval. Group posts never
reach the neighbourhood feed.

**Linked chat.** A Phase 3 group conversation whose membership follows the group
as people join, leave, are removed or are banned. It is managed only from the
group.

**Notifications:** invitations, join requests (to admins), approvals of requests
and of posts.

**Cross-cutting:**

- tiered limits on group creation and invitations, and a daily limit on join
  requests;
- group posts share the feed's daily post limit;
- export, purge and the retention job reach groups.

**Schema.** Migration 0009 adds `groups`, `group_members` and
`group_invite_links`. It turns `posts.group_id` and `conversations.group_id` into
real foreign keys, and adds post approval and pinning. It also widens the media
purpose and audit action constraints.

**Endpoints:** 30 new, 104 in total across 86 paths, plus the socket.

### Verified

| Check | Result |
| --- | --- |
| `pytest` | **912 passed** in 4½ minutes with coverage, **91% coverage**; the groups module 92–100% |
| `ruff` + `mypy` (strict, app and tests) | clean |
| Migrations | 0008 ↔ 0009 down and up twice; `alembic check` clean; 0009 applied on boot to the development database |
| Privacy scan | passes with the new group responses: area label and band only, and nothing for private groups |
| OpenAPI and session scope | every operation documented; every route commits before responding |
| Live stack | see below |

**The brief's acceptance criteria** (`tests/test_group_permissions.py`):

| Criterion | Result |
| --- | --- |
| Non-members can't read private groups | An outsider gets 404 for the group, its posts, its members, a post by ID, its comments, and joining, posting, commenting, reacting or reporting. An invitee sees the profile but not the posts. It never appears in discovery and stores no location |
| Members can't use admin tools | Every admin endpoint (20) called by every role (4). Each of the 80 cells asserts a 403 exactly when `MINIMUM_ROLE` says so, and never otherwise. Nobody acts on an equal or higher rank |
| Banned users can't rejoin | Refused by every route: joining, asking, preview, invite link, accepting an invitation, and leaving to reset. An admin's invitation gets 409 "unban them first". A ban works on non-members too. A banned user does not see the group in discovery. Unbanning lets them ask again |

**Live stack**, real API container:
- a public group is labelled "Nyeri Town", and the private one has no label;
- Kamau, 1.5 km away, finds it by searching "kamkwa estate", with band `1_3km`;
- the private group cannot be discovered;
- Kamau joins and posts, and the post stays out of a neighbour's feed;
- an outsider gets 404 on the private group, and a member using an admin tool gets
  403;
- once Wanjiku bans Kamau, his rejoin is a 404.

### Defects found, all fixed

1. **Alembic silently dropped a foreign key.** `groups.chat_conversation_id` →
   `conversations` (the two tables reference each other, so it is `use_alter`)
   was rendered inside `create_table`, and Alembic skips it there. Caught by
   `alembic check`; it is now created after both tables exist.
2. **The enum-constraint test matched too loosely.** Its `status` pattern also
   matched the new constraint on `approval_status`, and reported a false
   mismatch. It now matches the parenthesised column name.
3. **Autogenerate duplicated every new enum constraint**, as in 0007. Removed by
   hand.

### Deferred

With the phase that will own each; reasoning in `docs/DECISIONS.md`.

- **Moderators deleting comments** in their group — Phase 8.
- **Moving a public group** other than by switching visibility.
- **Group events** — Phase 7.
- **Ranked discovery** beyond membership size — Phase 8.
- **A linked-chat size cap**, and **join questions** — not needed yet.

### How to try it

```bash
docker compose up --build   # migration 0009 runs on boot

# Create a public group (placed at your home area)
curl -X POST localhost:8000/v1/groups -H "Authorization: Bearer $A" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Kamakwa Estate Residents","category":"estate","visibility":"public","join_policy":"approval"}'

# A neighbour finds it, typo and all, and asks to join
curl "localhost:8000/v1/groups/discover?q=kamkwa" -H "Authorization: Bearer $B"
curl -X POST localhost:8000/v1/groups/$GID/join -H "Authorization: Bearer $B"

# The owner approves, or makes a link for a private group
curl -X POST localhost:8000/v1/groups/$GID/members/$B_ID/review -H "Authorization: Bearer $A" \
  -H 'Content-Type: application/json' -d '{"action":"approve"}'
curl -X POST localhost:8000/v1/groups/$GID/invite-links -H "Authorization: Bearer $A" \
  -H 'Content-Type: application/json' -d '{"max_uses":20}'
```

### Open questions

- **HEIC support**, carried from Phase 2: add `pillow-heif`, or have the Flutter
  app send JPEG.
- **Development `SECRET_KEY`** (minor), carried from Phase 3: the dev default is
  29 bytes.
