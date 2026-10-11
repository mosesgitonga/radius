# Decisions

Decisions taken while building, with the alternatives that were considered.
Newest phase last. A decision here is not permanent — it is a record of what was
chosen and why, so that revisiting it later is an informed choice rather than an
accident.

Technology choices fixed by the build brief (FastAPI, PostGIS, H3, Redis, arq,
M-Pesa, and so on) are not relitigated here. Only decisions made during the build
are recorded.

---

## Phase 0 — Foundation

### 0.1 Structured logging on the standard library, not structlog

**Context.** The brief requires structured JSON logs with redaction, but
`structlog` is not in the agreed dependency list.

**Choice.** A `logging.Formatter` that emits JSON, plus two `logging.Filter`s for
redaction and request-ID stamping.

**Alternatives.** `structlog` would give nicer ergonomics (bound loggers,
processor pipelines) but needs a new dependency for what is about sixty lines of
stdlib code. `python-json-logger` is the same trade in smaller form. The stdlib
route also has a real advantage: filters attach to the **root** logger, so logs
from SQLAlchemy, asyncpg, and any future third-party library are redacted too —
something a parallel structlog pipeline would miss.

### 0.2 Redaction is a root-logger filter, not a calling convention

**Context.** The brief forbids phone numbers, OTPs, tokens, and coordinates from
reaching the logs. Relying on every future call site to remember is relying on
the one that forgets.

**Choice.** Two layers on the root handler: values of sensitive-looking **keys**
are replaced wholesale, and sensitive **patterns** (Kenyan and E.164 phone
numbers, JWTs, bearer tokens, coordinate pairs) are scrubbed from free text.
Exception tracebacks are scrubbed too, since they quote the offending input.

**Known gap, accepted.** A bare six-digit OTP or a lone coordinate cannot be
pattern-matched without also redacting page counts and durations. These are
covered by the key-name layer, which is why `app/core/logging.py` says to name
fields sensibly. Logging a bare secret as an unnamed positional value remains
possible; `tests/test_logging.py` pins everything that is covered.

### 0.3 `uvicorn-worker` added as a dependency

**Context.** The brief specifies "Gunicorn with Uvicorn workers in production".
Uvicorn removed `uvicorn.workers` in 0.54; the Gunicorn worker class now ships as
a separate package maintained by the same project.

**Choice.** Add `uvicorn-worker`. This is the agreed deployment model's current
packaging, not a new choice.

**Alternatives.** Pinning Uvicorn below 0.54 trades a supported release for an
unmaintained one. Running `uvicorn --workers N` directly needs no extra package
but gives up Gunicorn's worker supervision and graceful restarts, which is the
reason the brief asked for Gunicorn.

**Related.** Both `gunicorn` and `uvicorn-worker` carry a
`sys_platform != 'win32'` marker: Gunicorn imports `fcntl` and cannot even be
imported on Windows, where development happens. The Linux image installs them;
Windows development uses Uvicorn directly.

### 0.4 One session, one transaction, per request — committed by the dependency

**Context.** Something has to decide when work is committed.

**Choice.** `get_session` commits when the handler returns and rolls back when it
raises. Services may commit mid-request if they need to, but need not remember to
commit at the end.

**Alternatives.** Requiring every service to commit explicitly is more precise
but makes the failure mode "the write silently did not happen", which is both
easy to do and hard to notice. Autocommit per statement would make multi-step
operations non-atomic, which is unacceptable for messaging and payments.

### 0.5 Rate limiting: atomic fixed window, anchored at the first request

**Context.** The limiter guards OTP SMS spend, posting, and messaging. Concurrent
requests must not slip past it — a flaky mobile connection retrying is exactly
the case that sends five SMS messages.

**Choice.** A Lua script doing `INCRBY` plus a conditional `PEXPIRE` in one round
trip. The window starts at the first request in it, and the expiry is set only on
that first request, so hammering a saturated limit cannot extend it. Rejected
requests still count.

**Alternatives.** Read-then-write from Python has a race wide enough to drive
through. A sliding-window log is more accurate but stores one entry per request.
A token bucket allows controlled bursts, which is a better fit for feed reads but
a worse one for "five OTPs per day"; it can be added alongside if needed.

### 0.6 Rate limiter fails open by default, with per-endpoint opt-out

**Context.** What should happen when Redis is unreachable?

**Choice.** Allow traffic, log an error, and let individual endpoints pass
`fail_open=False`. OTP and payment endpoints will do so from Phase 1.

**Alternatives.** Failing closed everywhere turns a Redis blip into a total
outage. Failing open everywhere means an attacker who can disrupt Redis gets
unlimited OTP sends at the project's expense. Per-endpoint choice puts the
decision where the cost is known.

### 0.7 Cursor pagination by keyset, with unsigned opaque cursors

**Context.** Every list in the API is cursor-paginated.

**Choice.** Keyset pagination on `(sort_column, id)` using a row comparison, with
the cursor as unpadded URL-safe base64 of a small JSON object. Not signed.

**Alternatives.** Offset pagination makes deep pages expensive and, in a feed
that is constantly growing, shows duplicates and skips items. Signing cursors
would add key management for no benefit: a cursor only positions a query whose
filters and authorisation are applied server-side regardless, so a tampered
cursor can at worst show its sender a page they were already entitled to.

The ID tiebreaker is not optional. Posts in a neighbourhood feed really do share
a timestamp, and ordering by timestamp alone makes their relative order undefined
between queries, which is how rows end up on two pages or none.

### 0.8 Oversized page limits are clamped, not rejected

**Choice.** `?limit=10000` returns the maximum page size rather than a 422.

**Reasoning.** The cap exists to protect the server and the user's data bundle,
and both are achieved by clamping. A client that asks for too much is better
served with a sane page than with an error it has no handling for.

### 0.9 Validation errors report the field but never the value

**Context.** Pydantic's error objects include the offending input by default.

**Choice.** Return only `location`, `type`, and `message`.

**Reasoning.** On `POST /v1/me/location` the offending input is a pair of
coordinates, which the brief forbids from reaching logs at all — and error
responses are logged. Clients already know what they sent, so nothing is lost.
Smaller payloads also suit expensive mobile data.

### 0.10 Internal error details are never returned, in any environment

**Choice.** Unhandled exceptions produce `{"code": "internal_error", "message":
"Something went wrong"}` with no traceback, including when `DEBUG=true`.

**Reasoning.** A traceback discloses library versions and file layout. Debugging
information belongs in the logs, tied to the response by the `X-Request-ID`
header, which is set even on the 500 path. An environment-dependent error body
is also one more way staging and production differ.

### 0.11 Error classes carry the `Error` suffix

**Choice.** `NotFoundError`, `ForbiddenError`, `RateLimitedError`, and so on.

**Reasoning.** `NotFound` reads slightly better at the raise site, but the suffix
is the Python convention (and `ruff`'s `N818`), and a per-file lint exemption for
a naming preference is a bad trade.

### 0.12 Health and readiness are separate, and sit outside `/v1`

**Choice.** `GET /health` checks nothing and always answers if the process is
alive. `GET /ready` checks PostgreSQL and Redis concurrently and returns 503 with
per-dependency results. Both live at the root.

**Reasoning.** Conflating them means a Redis hiccup makes the orchestrator kill
and restart healthy containers, turning a degraded dependency into an outage.
They are operational endpoints, not client API, and their shape should not change
with an API version.

### 0.13 Extensions are enabled by migration, not by the image

**Choice.** Migration `0001` runs `CREATE EXTENSION IF NOT EXISTS postgis` and
`pg_trgm`.

**Alternatives.** The `postgis/postgis` image creates them in its init scripts,
which is convenient and invisible — right up to the first deploy onto a managed
PostgreSQL, where the schema then does not exist. Doing it in a migration makes
the schema reproducible anywhere and makes "migrations apply cleanly on an empty
database" literally true.

### 0.14 Pure ASGI middleware rather than `BaseHTTPMiddleware`

**Choice.** `RequestContextMiddleware` implements the ASGI interface directly.

**Reasoning.** `BaseHTTPMiddleware` does not see WebSocket connections, which
Phase 3 needs, and it wraps every response in an extra task, complicating
streaming and exception propagation.

### 0.15 `X-Forwarded-For` is not trusted unless configured

**Choice.** `TRUST_FORWARDED_FOR` defaults to false; the client IP comes from the
socket unless told otherwise.

**Reasoning.** Any client can send that header. Trusting it without a proxy that
overwrites it lets a caller forge a fresh IP per request and walk straight
through every per-IP rate limit.

### 0.16 CORS is off by default

**Choice.** No CORS middleware unless `CORS_ORIGINS` is set.

**Reasoning.** The clients are native apps and send no `Origin`. Enabling CORS by
default would grant browsers access for no benefit. The Phase 8 admin console
sets the variable.

### 0.17 Tests run against real PostgreSQL with PostGIS

**Context.** The brief requires it; this records why it is worth the setup cost.

**Choice.** A real PostGIS database, with per-test isolation by opening a
transaction on a connection, binding the session to it with
`join_transaction_mode="create_savepoint"`, and rolling back afterwards.
Migrations run once per session.

**Alternatives.** SQLite has no PostGIS, no `pg_trgm`, and different NULL and
type semantics; every radius test would be meaningless. Recreating the schema per
test is correct but far slower. Running `create_all` instead of migrations would
let a model with no migration pass CI.

### 0.18 The privacy schema scanner is in place from Phase 0

**Context.** The brief introduces it in Phase 1, when the first location field
exists.

**Choice.** Write it now, in `tests/test_privacy_schemas.py`. It walks the
OpenAPI document, collects every schema reachable from a response, and fails on
forbidden field names. It includes a self-test that adds a leaky model and
asserts the scan catches it — a privacy test nobody has watched fail is a privacy
test nobody should trust.

**Reasoning.** Costs nothing now and means the phase that introduces the first
coordinate field cannot introduce it quietly. Exact distance fields are on the
denylist alongside coordinates, since three spoofed positions and three exact
distances locate a home.

### 0.19 SQLAlchemy floor raised to 2.1

**Context.** SQLAlchemy 2.1 flattened `Select`'s type parameter: `select(Post)` is
`Select[Post]`, where 2.0 had `Select[tuple[Post]]`.

**Choice.** Require `>=2.1` and annotate for it. Still "SQLAlchemy 2.x" as the
brief specifies.

**Alternative.** Writing annotations that satisfy both versions means giving up
`strict` mypy on the pagination helper, which is the one place generic typing
earns its keep.

### 0.20 UUID primary keys generated in Python

**Choice.** `uuid4` as a Python-side column default, not `gen_random_uuid()`.

**Reasoning.** An object has its identity before it is flushed, which makes
building graphs of related rows in one transaction straightforward. Keeps the
keys non-enumerable, as required. UUIDv7 would index better by giving time-ordered
keys, but it is not in the standard library for 3.12 and would need a dependency;
worth revisiting at the Phase 8 performance pass.

### 0.21 All thirteen module packages scaffolded empty

**Choice.** `app/modules/<name>/__init__.py` exists for every module in the
brief, each with a docstring naming the phase that fills it.

**Reasoning.** The layout doubles as a map of the build, and it settles naming
once rather than per phase. No module has code before its phase.

---

## Phase 1 — Accounts, consent and location

Decided before the phase: this resolves the open question the brief raised for
Phase 0 to answer.

### 1.1 The feed is anchored to home, with "explore here" as a temporary mode

**Context.** The brief's open question: is the feed anchored to the user's home
location, their current location, or both?

**Choice.** Home, with an optional "explore here" mode, plus five supporting rules
the user added so the model holds together:

1. **Authored content always carries the author's home area**, even while
   exploring. Nobody can drop posts or listings into a neighbourhood they do not
   live in.
2. **Explore mode is session-only.** The explored location lives in Redis with a
   ~2 hour TTL and is never written to PostgreSQL. Other users only ever see the
   author's home area.
3. **Visitors are labelled.** A comment made while exploring still shows the
   author's home area, so locals can tell they are visiting.
4. **Home can be changed roughly once every 14 days.** Enough for a DeKUT student
   moving between campus and home over the holidays; not enough for a spammer to
   hop between neighbourhoods.
5. **The verified-neighbour badge stores only a daily yes/no** for "was this user
   in their home area today", never the location it was derived from.

**Why this is the right shape.** A current-location feed would make the product a
different thing — a travel app rather than a neighbourhood one — and would let
anyone spoof their way into any neighbourhood's feed, which is the attack the
radius system otherwise has no answer to. Anchoring to home also means a viewer's
feed is stable, which matters on expensive mobile data: the same anchor means the
same cached radius and no re-fetch because someone walked down the road.

**Why the rules matter more than the choice.** Each one removes something the
system would otherwise have to defend against or store:

- Rule 1 means content location needs no separate trust model; it inherits the
  home area, which is already rate-limited by rule 4.
- Rule 2 is data minimisation in the Data Protection Act sense — the explored
  location is never at rest in a durable store, so it cannot be exported,
  breached, or subpoenaed, and it needs no retention job.
- Rule 5 reduces fourteen days of location history to fourteen bits. That is the
  difference between holding a movement profile and holding a boolean.

**Implementation consequences for Phase 1.**

- The radius engine takes an **explicit anchor cell** rather than reading the
  user's row, so the same code serves the home feed and explore mode.
- Explore locations get the same spoof detection, rate limiting, and redaction as
  home locations. Being ephemeral does not make them less sensitive in flight.
- The daily home-presence flags are stored as a small fixed-width bitmask on the
  user row rather than a row per day: it is cheaper, and it self-purges, so
  retention needs no job.
- `is_visiting` is a boolean on authored content, not a location. It reveals only
  "not currently at home", which is not a position.

**Open sub-questions**, carried into the Phase 1 plan:

- Do exploring users count toward `cell_activity` density in the cell they are
  exploring? **Recommended: no.** Density should reflect residents. Counting
  visitors would let a crowd at a market or a campus event shrink every local
  resident's radius for the afternoon, which is the opposite of what the adaptive
  radius is for.
- Does changing home reset the badge's 14-day consistency window?
  **Recommended: yes**, with vouching by two verified neighbours as the fast path
  back, since the window measures consistency with a home area that has changed.
- Do explore days count as "not home" for the badge, or are they excluded?
  **Recommended: excluded** — neither a yes nor a no. The brief already counts
  only *active* days, and penalising a deliberate, temporary mode would make the
  badge measure "did not travel" rather than "lives here".

---

### Deferred from Phase 0, with the phase that will own it

- **`Idempotency-Key` handling.** An API convention in the brief, but Phase 0 has
  no create endpoints to retry. The storage (a keyed record of request hash and
  response) lands in Phase 2 with the first `POST` that creates content.
- **Authentication and `request.state.user_id`.** The rate limiter already reads
  it and falls back to the IP; Phase 1 populates it.
- **Radius, H3, and k-anonymity settings.** Config entries land with the engine
  that reads them, in Phase 1.
- **`ETag` support.** Cheap only on resources that rarely change; none exist yet.
  Phase 4's group metadata and Phase 1's area list are the first candidates.
- **Production deployment notes, backups, metrics, and error tracking.** Phase 8,
  per the brief.
