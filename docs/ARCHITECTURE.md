# Architecture

MyRadius is a **modular monolith**: one FastAPI application, one PostgreSQL
database, one Redis, one background worker, deployed as a couple of containers on
a single VPS. That is a deliberate choice for a product launching in one town with
low initial density — the operational cost of microservices would exceed the
engineering cost of the whole backend. Module boundaries are enforced in code
rather than by the network, so splitting something out later is a refactor, not a
rewrite.

## The pieces

```
                   Flutter clients (Android, iOS)
                              │ HTTPS
                     ┌────────▼────────┐
                     │  Caddy / Nginx  │   TLS, X-Forwarded-For
                     └────────┬────────┘
                              │
                    ┌─────────▼─────────┐
                    │  api (FastAPI)    │   Gunicorn + Uvicorn workers
                    └───┬───────────┬───┘
                        │           │
          ┌─────────────▼──┐   ┌────▼──────────────────────┐   ┌──────────────┐
          │ PostgreSQL 16  │   │ Redis                     │   │ Object store │
          │ PostGIS 3      │   │ cache, rate limits, OTPs, │   │ R2 (prod)    │
          │ pg_trgm        │   │ pub/sub, arq queue        │   │ SeaweedFS    │
          └─────────────▲──┘   └────▲──────────────────────┘   │ (dev)        │
                                                                └──────▲───────┘
                  phones upload images straight to the object store ───┘
                        │           │
                    ┌───┴───────────┴───┐
                    │ worker (arq)      │   schedules and queued jobs
                    └───────────────────┘
```

PostgreSQL is the source of truth. Redis holds only data that can be rebuilt:
cached radii, rate-limit counters, pending OTPs, pub/sub fan-out, and the job
queue. Losing Redis degrades the service; losing PostgreSQL loses data.

## Code layout

```
app/
  main.py          application factory, middleware and router wiring
  core/            cross-cutting concerns, knows about no feature
    config.py        settings, including the radius table
    context.py       request-scoped context (request ID)
    db.py            engine, sessions, unit of work
    models.py        declarative base and shared mixins
    errors.py        error envelope and exception handlers
    logging.py       structured logging and redaction
    middleware.py    request ID and access log
    pagination.py    cursor pagination
    ratelimit.py     Redis rate limiter
    realtime.py      pub/sub publish, the per-process hub, presence
    redis.py         Redis client and the cache-error set
    security.py      codes, keyed hashing, access tokens
    health.py        liveness and readiness
    providers/       external services, each behind a Protocol: SMS, storage, push
  modules/
    auth/            phone.py, service.py, dependencies.py, router.py
    users/           service.py (incl. the shared blocking check), tasks.py
    location/        cells.py, radius.py, service.py, tasks.py
    messaging/       service.py, views.py, router.py, socket.py (the `/v1/ws` endpoint)
    notifications/   service.py (devices, preferences, collapsing, push), router.py
  modules/<name>/  one package per feature area
    models.py        SQLAlchemy models
    schemas.py       Pydantic request and response models
    service.py       business logic
    router.py        HTTP endpoints, thin
  workers/         arq settings and tasks
```

Three rules keep the monolith from congealing:

1. **Routers are thin.** They validate, call one service function, and shape the
   response. Anything else belongs in `service.py`.
2. **Modules talk through service functions**, never through each other's routers
   or by reaching into each other's tables. The feed module asks the users module
   "is A blocked by B?"; it does not query `blocks` itself.
3. **Shared rules live in exactly one place.** Blocking checks, location privacy,
   and rate limits are imported, not reimplemented. A rule with two
   implementations has one that is wrong. Concretely, as of Phase 1:
   `users.service.blocked_user_ids` is the only blocking rule,
   `location.cells.locate` is the only way coordinates become storable,
   `location.service.area_label_for_cell` is the only way a place gets a name,
   `moderation.audit.record_audit_event` is the only way anything is audited,
   `media.images.process_image` is the only way an image becomes servable, and
   `feed.service.visible_to` is the only definition of who may see moderated
   content. Phase 3 adds `messaging.service.audience` as the only definition of
   who hears about something in a conversation, and `notifications.service.notify`
   as the only way a notification is created.

   One deliberate exception to rule 2: moderation resolves a reported message by
   querying the messaging *models*, because the messaging service imports
   moderation (block-and-report) and the reverse import would be a cycle. The
   query applies the same visibility rules as reading.

`core` may not import from `modules`. That is what makes it reusable and keeps
import cycles impossible.

## Request lifecycle

1. **`RequestContextMiddleware`** assigns or adopts a request ID, resolves the
   client IP, and logs one access line on the way out. Pure ASGI, so it covers
   the WebSocket connections arriving in Phase 3.
2. **Route dependencies** run: authentication (Phase 1), rate limits, pagination
   parameters, and a database session.
3. **The session is a unit of work.** It commits if the handler returns and rolls
   back if it raises, so a handler cannot half-apply a change. The commit, and the
   realtime events and job enqueues registered with `after_commit`, finish
   **before** the response is sent: routes take `core.db.SessionDep`, which is
   function-scoped (decision 3.22).
4. **Errors** become the single envelope. Service code raises `AppError`
   subclasses; the handlers in `core/errors.py` do the translating. Unhandled
   exceptions are logged with a traceback and returned as a bare 500.
5. **Logs** are JSON, one object per line, carrying the request ID. A redaction
   filter on the root logger scrubs phone numbers, tokens, and coordinates
   regardless of which code emitted them.

## Location and the adaptive radius

This is the part of the system worth designing carefully, because it is both the
product's differentiator and its largest privacy risk.

**Storage.** Clients send latitude and longitude. `location.cells.locate` converts
them to an H3 resolution-9 cell immediately and returns a `CellLocation`; nothing
downstream can recover the input. The cell ID is stored as `BIGINT` with a B-tree
index, alongside its centroid as `geography(Point, 4326)` with a GiST index, and
the res 8, 7 and 6 parents that density aggregation and k-anonymity need. Raw
coordinates are never persisted and never logged.

**No location history exists.** A home change overwrites. Explore-mode locations
live in Redis for about two hours and never reach PostgreSQL. The fourteen days of
"was this user in their home area" that the verified badge needs is a 14-bit
integer on the user row — a fact, not a movement profile. See decision 1.1.

**The radius** is computed per res-8 cell rather than per user, so it can be
cached and shared by everyone in that cell. A worker refreshes a `cell_activity`
table every fifteen minutes. To compute a radius, the engine expands H3 rings
outward from the viewer's cell, accumulating active-user counts until it reaches
the feature's target, converts the ring distance to kilometres using real H3 cell
distances, and clamps to the feature's bounds. Results are cached in Redis per
`(feature, cell)` with hysteresis: a cached radius is only replaced when the new
value differs by more than 20%, so feeds do not jump about between refreshes.

Only the feed radius is computed. The other four features are multiples of it,
clamped to their own bounds, so the expensive part happens once and every feature
widens and narrows together as an area fills up.

**Querying** is then `ST_DWithin(content.geog, viewer_centroid, radius_m)`,
answered from the GiST index.

**Privacy** is enforced at the schema layer, not by convention. Public response
models have no coordinate, centroid, or fine-grained cell field, and
`tests/test_privacy_schemas.py` walks the generated OpenAPI document on every CI
run and fails if one appears. Positions reach clients as an **area label**
(the finest H3 parent containing at least K registered users, named from a seeded
`areas` polygon) and a **distance bucket** (`under_1km`, `1_3km`, `3_10km`,
`10km_plus`). Buckets rather than exact distances specifically to defeat
trilateration: an attacker who can spoof their own location and read exact
distances can locate someone's home in three requests.

## Realtime

```
 phone A ──ws──▶ api worker 1                       api worker 2 ◀──ws── phone B
                  │  POST /messages                      ▲
                  │  1. lock conversation row,           │ 4. hub hands the event
                  │     seq = last_seq + 1               │    to B's socket
                  │  2. commit                           │
                  │  3. after commit: PUBLISH ──▶ Redis rt:user:B ──┘
                  │                                      (subscribed by worker 2,
                  ▼                                       because B is connected there)
           arq: deliver_message_push ──▶ for members not in presence:* ──▶ FCM
```

**Persist, then publish.** A message is committed before anyone hears of it, and
what they hear is only "conversation X has seq N". Every client — live,
reconnecting, or freshly installed — then does the same thing: fetch messages after
the last seq it holds. Pub/sub can drop events, and that costs nothing, because the
database is the record.

**One hub per process.** Each API process subscribes, over a single Redis
connection, to the channels of the users connected to it. That is why delivery
works with any number of workers, and why a worker can crash without losing
anything but its sockets, whose clients reconnect and sync.

**Pushes are for people who are not looking.** The push job runs in the worker and
checks presence at delivery time, so someone who opened the app in the meantime is
not buzzed. A muted conversation, a block, or the throttle also stop a push;
`notifications.service.push_to_user` is the one place those rules live.

**Sockets follow their session.** Signing out publishes `session.revoked` and the
socket closes, on whichever worker holds it. A token expiring closes the socket
unless the client has re-authenticated in-band.

## Media

Images never pass through the API. A client asks for a presigned PUT, uploads
directly to storage, and confirms; the worker then fetches the original, validates
it from its magic bytes, re-encodes it to WebP at 320, 720 and 1280 px from decoded
pixels — which is what guarantees no EXIF survives — stores the variants, and
deletes the original. Only processed variants are ever addressable.

Two safety nets keep this from leaving users stranded: enqueueing is best-effort,
with a five-minute catch-up sweep for lost jobs, and abandoned uploads are swept
after a day.

## Scaling, when it is needed

In rough order, and none of it yet:

1. More Uvicorn workers, then a bigger VPS.
2. A PostgreSQL read replica for feed and search queries.
3. Separate worker processes per queue, once one kind of job can starve another.
4. Extract a service only when a module has genuinely divergent scaling needs —
   realtime messaging is the plausible first candidate. The hub already talks only
   to Redis, so moving `/v1/ws` to its own process is a deployment change: run the
   same app with only the socket router mounted.

The module boundaries above are what make step 4 cheap. They are worth keeping
clean even while the answer is "one box is fine".
