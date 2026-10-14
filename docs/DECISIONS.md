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

### 1.2 Verification does not reveal whether an account exists

**Context.** `POST /v1/auth/otp/request` could usefully tell the client whether
to show a sign-in or a sign-up screen.

**Choice.** It does not. The response is byte-for-byte identical either way, and
`verify_otp` returns either a token pair or a signup token.

**Reasoning.** Anything that differed would make the endpoint an oracle for "is
this number registered?". For a neighbourhood product that is a way to find out
who lives in a place, which is the attack the rest of the privacy design exists to
prevent. The client gets its answer one step later, from verification, by which
point the caller has proved they control the number.

### 1.3 Refresh tokens rotate, and reuse revokes the whole family

**Context.** Rotation is safer than long-lived refresh tokens, but it creates an
ambiguity: a client whose network drops after the server rotates and before it
stores the new token will retry with a token the server has already consumed —
which is indistinguishable from an attacker replaying a stolen one.

**Choice.** Every session records the session it replaced and the `family_id` of
the sign-in it descends from. A consumed token presented again revokes the entire
family.

**Alternatives.** Allowing a short replay window would make theft undetectable
during that window. Revoking only the presented token would leave the thief's
current token working. Revoking the family means a thief loses what they stole,
and an honest user on bad mobile data signs in once more instead of being quietly
shadowed by somebody holding a live token — which is the right way round.

**One subtlety, found by a test.** The revocation has to be committed before the
error is raised, because the request session rolls back on an exception. Without
that commit the mechanism produced exactly the failure it exists to prevent.

### 1.4 Access tokens name their session, and it is checked every request

**Choice.** The access token carries a session ID, and `get_current_user` loads
that session to confirm it is still live. One indexed lookup per authenticated
request.

**Reasoning.** Without it, "sign out this device" would not take effect until the
access token expired — so a stolen phone would keep working for another fifteen
minutes after its owner revoked it. Fifteen minutes is exactly when that matters.

### 1.5 Suspended accounts keep read access

**Choice.** `CurrentUser` admits suspended and deletion-pending accounts;
`ActiveUser` is required for anything that creates or changes something.

**Reasoning.** A suspension restricts what somebody may do, not whether they may
look. Blocking sign-in outright would also stop them reading why they were
suspended, exporting their data, or cancelling a deletion — all of which they are
entitled to do.

### 1.6 Only the feed radius is computed; the rest are multiples

**Context.** Five features each have a radius.

**Choice.** The density walk runs once, for the feed. Every other feature is the
feed radius times its configured multiplier, then clamped to its own bounds.

**Reasoning.** It is what the brief specifies, and it has a property worth
keeping: every feature widens and narrows together as an area fills up, rather
than drifting apart. It also means the expensive part happens once.

The multiplier applies to the feed's *clamped* radius, because "2 × feed radius"
means twice the radius the feed actually uses. The unclamped ring distance is kept
alongside it purely so a result can report honestly whether a clamp was applied.

### 1.7 One cache entry per cell, not per (feature, cell)

**Context.** The brief says to cache per `(feature, cell)` with hysteresis.

**Choice.** Cache the feed radius per cell, with hysteresis, and derive the other
four arithmetically from the stabilised value.

**Reasoning.** The outcome is identical — the derived radii are pure functions of
the feed radius, so stabilising it stabilises all of them — with one Redis round
trip instead of five. On an API serving mobile clients over expensive data, four
avoided round trips per radius read is worth more than the symmetry.

### 1.8 The density query is bounded by the feed maximum, not the ring cap

**Choice.** The disk fetched for the ring walk reaches the feed's configured
maximum (about 17 res-8 rings for 15 km), not `radius_max_ring_k` (40).

**Reasoning.** A radius can never exceed its maximum, so counting users beyond
that distance is work whose result is discarded. `radius_max_ring_k` stays as a
safety bound against a sparse-data bug becoming an unbounded loop.

### 1.9 Ring distances come from H3 geometry, never a constant

**Choice.** `ring_distance_km` measures real centroid-to-centroid distances and
adds half a cell diameter.

**Reasoning.** The brief asks for this, and the reason is concrete: H3 cells are
not uniform. Their size varies with latitude and with the icosahedron's
distortion, so a hardcoded "1.2 km per ring" would be wrong by a different amount
in Nyeri than in Nairobi — and the radius a user is shown would not match the one
actually being queried.

### 1.10 Area labels fail towards privacy

**Choice.** Registered counts come from the precomputed `cell_activity` table,
and a missing row counts as zero.

**Consequence, accepted.** A freshly deployed system whose density worker has not
yet run labels everyone generically ("nearby") rather than precisely. That is the
safe direction to fail in: the alternative is naming a place that fewer than K
people share. The density cron runs at startup partly to shorten that window.

### 1.11 Spoofing is flagged, not refused

**Choice.** An implausible jump between home locations is recorded as an audit
event and reported in the service result, but the update succeeds. The response
says nothing about it.

**Reasoning.** The brief asks for implausible jumps to be *flagged*. Refusing
would lock out anyone who genuinely flew between Kenyan towns while doing almost
nothing to stop a patient attacker, who need only wait. The real control is the
home-change cooldown. Telling the caller they were flagged would only teach them
what to avoid next time.

### 1.12 The home-change cooldown is the anti-abuse control

**Choice.** An established home area can be changed about once a fortnight;
setting one for the first time is free, and re-reporting the same cell is not a
change.

**Reasoning.** It is what stops someone hopping between neighbourhoods to spam
each in turn, and it is cheap to enforce. A fortnight is loose enough for a DeKUT
student moving between campus and home over the holidays, which is the case the
launch market actually contains.

### 1.13 Explore mode never reaches PostgreSQL

**Choice.** The explored anchor lives in Redis with a ~2 hour TTL. Authored
content still carries the author's home area, and other users always see the home
area.

**Reasoning.** Where somebody is *right now* is the most sensitive thing this
system could hold. Keeping it only in an expiring cache means there is nothing to
export, nothing to purge, no retention job, and nothing to disclose in a breach —
a stronger guarantee than any access control over a stored column. It also means
explore mode cannot be used to post into a neighbourhood somebody does not live
in, which removes a whole class of abuse.

### 1.14 Presence is one bit per day, recorded when a location is reported

**Choice.** `home_presence_mask` is a 14-bit integer, bit 0 being today. It is
rolled and set in the request that reports a location, and a nightly worker ages
stale masks forward.

**Reasoning.** The badge rule needs "was this user in their home area on at least
10 of the last 14 days". Fourteen bits answers that. Fourteen stored positions
would answer it too, and would also be a movement profile — the difference
between holding a fact and holding a dossier.

Comparison is at resolution 7, not 9: "in my home area" means the neighbourhood,
and someone at the shops two streets away is at home in every sense that matters.
Explore days set no bit and are therefore excluded rather than counted as absence.

### 1.15 A purged account holds no phone number

**Choice.** `phone_number` is nullable and set to null on purge.

**Alternatives.** The first implementation wrote a `purged:<uuid>` placeholder to
keep the unique index satisfied. It did not fit the column, so the retention job
failed on its first real account and nothing was ever purged — a compliance
failure whose only symptom was a worker log line. Widening the column would have
worked; null is better. It is the honest record of an erasure, PostgreSQL permits
many nulls in a unique index, and there is no width left to get wrong.

### 1.16 Enums are check constraints, not PostgreSQL enum types

**Choice.** `sa.Enum(..., native_enum=False)` renders closed sets — account
status, consent type, revocation reason — as VARCHAR with a CHECK constraint. The
product's *configurable* lists (post categories, group categories, service
categories) will be plain strings with no database constraint at all.

**Reasoning.** Adding a value to a native enum needs `ALTER TYPE`, which is
awkward to reverse and historically could not run inside a transaction. A check
constraint is an ordinary, reversible migration. And the brief requires new towns
and categories to work from configuration and seed data alone, which a database
enum would quietly prevent.

**Correction, Phase 3.** As first written, this entry was not true: the CHECK
constraints did not exist. SQLAlchemy has not emitted them by default since 1.4,
and `create_constraint=True` was never set, so for the whole of Phases 1 and 2
every enum column accepted any string. This came to light when a direct insert of
`status = 'definitely_not_a_status'` into `users` succeeded; the ORM would only
have noticed on the next read of that row, by failing it. Every enum column now
goes through `app.core.models.db_enum`, which sets the flag. Migration 0006 adds
the fourteen missing constraints. `tests/test_schema_integrity.py` checks that
every enum column has a constraint allowing exactly the values its Python enum
declares, so a value added without a migration fails in CI rather than in
production.

### 1.17 Per-user rate limiting lives in the auth module

**Choice.** `auth.dependencies.user_rate_limit` wraps the core limiter for
authenticated routes, instead of using `core.ratelimit.rate_limit(scope="user")`.

**Reasoning.** Dependency ordering. FastAPI resolves route-level dependencies
before path parameters, so a limiter that read `request.state.user_id` would run
before authentication had set it and silently fall back to counting per IP — a
per-account limit that was quietly per-address. Depending on `CurrentUser` makes
the order explicit rather than accidental.

### 1.18 A blocked user gets 404, not 403

**Choice.** `GET /v1/users/{id}` returns "not found" when either party has
blocked the other.

**Reasoning.** A distinct "you are blocked" response would let somebody confirm
a block — and the obvious next move is to ask a friend to look instead. 404 is
the same answer they would get for an account that never existed.

### 1.19 The displayed radius is rounded

**Choice.** `GET /v1/me/radius` rounds to the nearest half kilometre, and the
neighbour estimate to the nearest ten.

**Reasoning.** An exact radius is a direct read-out of how dense the caller's own
immediate area is, and an exact count is a count of identifiable people. Both are
information about the caller's neighbours rather than about the caller. Rounding
also gives the app the string it actually wants to show: "Your radius: 3 km".

---

### Deferred from Phase 1, with the phase that will own it

- **Avatars.** `users.avatar_media_id` exists and stays null until Phase 2
  creates the media table; it becomes a foreign key then, which is a constraint
  rather than a table rewrite.
- **Reach estimates on content.** Phase 2, with the first posts. The density data
  they need is already in `cell_activity`.
- **Badge computation.** Phase 8. Phase 1 records the presence bits it will read.
- **Moderation queue and moderator roles.** Phase 8. The audit table is here
  because data-rights actions need auditing from the first day there is personal
  data.
- **Restrictions and ban lists.** Phase 8. `UserStatus` already carries
  `suspended` and `banned`, and the dependencies enforce them.
- **Vouching.** Phase 8, as the fast path back after a home change resets the
  badge window.

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

---

## Phase 2 — Media and neighbourhood feed

### 2.1 Clients upload directly to storage; the API never handles image bytes

**Choice.** `POST /v1/media/uploads` returns a presigned PUT. The client uploads
straight to storage, then calls `/complete`; the worker fetches the bytes from
storage to process them.

**Reasoning.** A 12 MB photo on a slow Kenyan connection can take a minute, and a
minute of a Uvicorn worker is a minute nobody else gets served. The cost is that a
client can abandon an upload between presign and confirm, which the sweeper
handles — cheaper than any scheme to prevent it.

### 2.2 Images are re-encoded, not edited, and the original is deleted

**Choice.** Processing decodes the pixels and writes new WebP files. The EXIF
orientation is applied first, then everything is discarded. The original is
deleted once every variant is stored.

**Alternatives.** Stripping EXIF tags in place is fragile: metadata also lives in
XMP, IPTC, maker notes and ICC profiles, and an editor that removes the tags it
knows about leaves the ones it does not. Writing a new file from pixels is the only
approach where nothing can come along by accident. Verified with a photo carrying
four GPS tags, a camera make and a timestamp: none of it reaches any served byte,
and no original remains in the bucket.

### 2.3 The real content type comes from magic bytes

**Choice.** The declared type is recorded as a claim; the type that counts is
detected from the file. Pixel count is checked from the header before decoding,
and `verify()` runs before `load()`. SVG, GIF and TIFF are refused.

**Reasoning.** SVG is a document that can carry script. TIFF is large and a
historic source of decoder bugs. An animated GIF avatar is a feature nobody asked
for and a decoder surface nobody wants. A decompression bomb — a few kilobytes
expanding into gigabytes — is caught from its dimensions before any allocation.

### 2.4 Posts carry their own location, taken from the author's home

**Choice.** Each post stores the author's home cells and centroid at posting time.

**Reasoning.** It makes the feed one `ST_DWithin` on one GiST index rather than a
join against the users table, which changes constantly. And it keeps a post where
it was made: a user who moves does not drag two years of posts into a
neighbourhood that never saw them.

### 2.5 Reach is estimated from the post's own cell radius

**Context.** The brief defines reach as the active users in every cell whose own
radius covers the post, and allows an approximation.

**Choice.** Sum the active users within the post's *own* cell's feed radius.

**Reasoning.** Radius tracks density and density varies smoothly over a few
kilometres, so neighbouring cells have radii close to the post's — the exact answer
and the approximation agree closely. The exact version would need a radius per
surrounding cell; this one costs one cached radius and one indexed read. Rounded to
the nearest ten, because an exact figure is a headcount of who lives nearby.

### 2.6 Counters change in SQL; concurrent inserts use savepoints

**Choice.** `UPDATE ... SET count = count + 1`, never read-modify-write. Reactions,
story views and reports insert inside `begin_nested()`, and an integrity error from
the unique index is treated as "already done".

**Reasoning.** Two neighbours reacting at once would otherwise both read 4 and both
write 5. And a double tap on a laggy connection is ordinary, not exceptional — it
should produce one row, not an error.

### 2.7 Every refusal to read is a 404

**Choice.** Blocked, hidden, removed, deleted and nonexistent all return the same
"not found".

**Reasoning.** A distinct answer for "blocked" would confirm a block, and one for
"hidden" would tell a reporter their report worked — both are information the
caller has no business having.

### 2.8 Hidden content stays visible to its author

**Choice.** Content hidden pending review is invisible to everyone except its
author, who sees it with `status: hidden_pending_review`.

**Reasoning.** Being made invisible without being told is worse than being told, and
the author is the one person for whom seeing it cannot amplify whatever got it
reported.

### 2.9 Auto-hide needs distinct, trusted reporters — and never applies to people

**Choice.** Content is hidden after `REPORT_AUTO_HIDE_THRESHOLD` reports from
distinct trusted reporters. Trusted, until Phase 8's badge exists, means: at least
7 days old, phone verified, active, and nothing upheld against them. A verified
neighbour is trusted outright. Reports against *users* never auto-act.

**Reasoning.** Each word closes an abuse. *Distinct* stops one person silencing a
neighbour by reporting repeatedly; *trusted* stops a burst of fresh accounts doing
the same. Untrusted reports still reach the queue for a human. People are excluded
because auto-suspending a person on three reports hands a coordinated group a way
to silence someone, whereas hiding a post is narrow and reversible.

### 2.10 Reports target a type and a UUID; reactions use real foreign keys

**Choice.** Opposite designs in the same phase.

**Reasoning.** Reactions have two targets, and a reaction must not outlive its post,
so real referential integrity is worth more than openness. Reports already foresee
nine target types across the phases, and must *outlive* what they concern — a
report about a purged post is still the record of why something happened.

### 2.11 Idempotency lives in PostgreSQL, in `core`

**Choice.** `Idempotency-Key` is supported on post, comment and story creation. The
claim is an INSERT against a unique index on `(user_id, endpoint, key)`, committed
before the work runs; the response is stored and replayed with
`Idempotent-Replayed: true`. A reused key with a different body is a 422. A failed
action's claim is released after rolling back.

**Alternatives.** Redis would be faster, but a flushed Redis would let a replay
create a second post — precisely what this exists to prevent. The stored response
has to be as durable as what it describes. Keys are scoped per user and endpoint so
one client cannot collide with, or probe for, another's.

### 2.12 Enqueueing is best-effort, backed by a catch-up sweep

**Choice.** If Redis is unavailable, `/complete` still succeeds and the asset waits
in `processing`; `catch_up_media` runs every five minutes and processes anything
stuck for more than two.

**Reasoning.** A user's upload should not fail because the queue blinked, and a job
that never runs is prevented by the sweep rather than by refusing the request.

### 2.13 Expired stories are deleted, not hidden — unless reported

**Choice.** `expire_old_stories` hard-deletes stories past their 24 hours, and their
stored images. A story under a pending report is kept until it is reviewed. Reads
also filter on expiry, because the sweep is not instant.

**Reasoning.** "Gone in a day" is a promise about data, not about visibility. But
deleting on schedule would destroy the evidence for exactly the stories most likely
to need it.

### 2.14 A post's author may delete comments under it

**Choice.** A comment can be deleted by its own author or by the author of the post
it is on.

**Reasoning.** A neighbour asking about a lost child or a burst pipe should be able
to clear abuse from beneath their own post without waiting for a moderator.

### 2.15 New accounts get tighter limits, on a shared counter

**Choice.** Below `NEW_ACCOUNT_AGE_HOURS` (48), an account gets 5 posts a day, 10
comments an hour, 3 stories a day; established accounts get 30, 60 and 10. Both
tiers count against one bucket. A config where the new-account limit exceeds the
established one is refused at boot.

**Reasoning.** A throwaway account is the cheapest spam vector there is, and age is
the one signal that cannot be faked without waiting. One counter means crossing the
threshold mid-window does not hand out a fresh allowance.

### 2.16 The feed response carries a `sort` field with one value

**Choice.** `FeedOut.sort` is always `"recent"` today.

**Reasoning.** The brief asks for ranking to be addable without changing the API.
A field the client already reads is how that happens; the query side only needs a
different ordering and cursor key.

### 2.17 SeaweedFS for development storage

**Context.** The plan said MinIO. `minio/minio` no longer pulls from Docker Hub —
MinIO stopped publishing community images in 2025 — and `quay.io/minio/minio` is
also unavailable.

**Choice.** SeaweedFS, which is Apache-licensed and speaks S3 with SigV4
presigning. Production uses Cloudflare R2; the application code is identical.

**Alternatives.** LocalStack also pulled, but is 1.6 GB against SeaweedFS's 724 MB
and has had licensing changes of its own. Running storage only in memory locally
would have left the presigned-upload flow unexercised until production.

**Consequence.** Presigning needs its own endpoint setting
(`STORAGE_PRESIGN_ENDPOINT_URL`). Inside Docker the API reaches storage as
`storage:8333`, which no phone can resolve, and the host is part of a SigV4
signature, so a URL cannot be rewritten after signing.

### 2.18 The audit writer is its own module

**Choice.** `record_audit_event` moved from `moderation/service.py` to
`moderation/audit.py`, which imports only `core` and the model.

**Reasoning.** Every module audits. When reports needed the users service, the old
location made `users.service → moderation.service → users.service` a cycle. A
module everyone imports must import nothing that imports it back.

### 2.19 `created_at` has a Python default as well as a server default

**Choice.** `TimestampMixin.created_at` gets `default=datetime.now(UTC)` alongside
`server_default=now()`.

**Reasoning.** PostgreSQL's `now()` is the time the *transaction* started, so every
row inserted in one transaction shares a timestamp and newest-first falls back to
the random UUID tiebreaker. Requests are one transaction each, but seed scripts,
imports, and Phase 8's half-million-post load are not. No migration was needed: the
server default is unchanged and still stamps raw SQL.

### 2.20 Each module exports and purges its own data; the router and task assemble

**Choice.** `feed.service.export_author_content`, `media.service.export_owner_media`,
`feed.service.purge_author_content` and `media.service.purge_owner_media` each own
their module's data. The export endpoint and the purge task call all of them.

**Reasoning.** The users service cannot do it itself — the feed imports it — and the
data-map rule is that a phase adding personal data extends export and erasure in the
same change. This shape makes that one new function per module.

---

### Deferred from Phase 2, with the phase that will own it

- **Feed ranking.** The `sort` field and query shape are ready; Phase 8's
  performance work is the natural time, with real engagement data.
- **Notifications for comments, replies and reactions.** Phase 3, as the brief
  specifies.
- **The moderation queue and actions.** Phase 8. Reports are filed, prioritised and
  auto-hide works; nobody can yet resolve one.
- **Hard purge of soft-deleted content.** Phase 8's retention job, with the 90-day
  window in the data map.
- **A CDN for images.** `STORAGE_PUBLIC_BASE_URL` is supported; until it is set,
  images are served by presigned GET, which works but defeats caching.
- **HEIC/HEIF.** Refused at presign. Pillow cannot decode it without the
  `pillow-heif` plugin, which is not in the agreed dependencies. An earlier draft
  accepted it at presign, so the user paid to upload a file that was then rejected
  as "could not be read"; refusing up front lets the client convert first. Android
  cameras produce JPEG; iPhones default to HEIC, so this matters for iOS. **Open
  question** — see the Phase 2 summary.
