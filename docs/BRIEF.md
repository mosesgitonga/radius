# MyRadius Backend — Build Brief

> Committed verbatim as the authoritative statement of what is being built and
> why. Everything in `docs/DECISIONS.md` either implements a choice made here or
> records a departure from it, with the reasoning. Where this document and the
> code disagree, this document is the intent and the disagreement is a bug.

---

## 1. The product

**Tagline:** Everything happening around you: your neighbours, local sellers, services, groups, and events, all within your radius.

MyRadius connects people with their neighbours and their local area. It replaces what people currently piece together from WhatsApp groups, Facebook pages, and word of mouth: what's happening nearby, who's selling what, who can fix a leaking tank, and where this weekend's events are.

**The core idea, and what makes it different:** every user has their own radius, which adjusts automatically to how many active users are nearby. In busy, dense areas the radius shrinks so the feed stays truly local. In quieter areas it widens so there's always something to see. The radius has sensible minimum and maximum limits. Some features (marketplace, services, events) reach further than the social feed, and users can adjust those with distance filters.

**Features in the full product:**

- **Neighbourhood feed:** posts and stories from people around you.
- **Messaging:** one-on-one chats, group chats, and buyer–seller chats, with message requests to keep out spam.
- **Groups:** public groups that are discoverable nearby, and private invite-only groups, with admin tools.
- **Marketplace:** sell items to neighbours who can pick them up nearby.
- **Jobs and services:** post that you offer a service, need one, or are hiring, with reviews so good providers stand out.
- **Events:** parties, church events, sports, harambees, clean-ups, with RSVPs, reminders, and location privacy for private gatherings.

**Safety and trust:** exact locations are never shown; users appear only in approximate areas. Verified-neighbour badges, ratings, reporting, blocking, and job-scam protections help people trust who they're dealing with. The system must comply with Kenya's Data Protection Act, 2019.

**Clients:** a Flutter app for Android and iOS is built separately. You are building only the backend API. Assume most users are on mid-range Android phones with slow, expensive mobile data.

**Launch plan:** launches in Nyeri, anchored by students at Dedan Kimathi University of Technology (DeKUT), local businesses, churches, chamas, and community groups. Then Karatina, Othaya, and Nanyuki, and later Nairobi. Nothing in the code may hardcode Nyeri; new towns must work by configuration and seed data alone. Early on, density will be low, so most users will sit at the maximum radius. That is expected.

**Business model:** revenue comes first from featured service providers and promoted listings, then from business profiles, marketplace fees, and event ticketing. Payments will use M-Pesa.

---

## 2. Goals for the backend

1. **Location features that are correct and private.** The radius system is the product's main differentiator and its biggest privacy risk. Treat it as the core of the system.
2. **Safe by default.** Privacy, blocking, reporting, and rate limiting are built in from the first phase that needs them, not added at the end.
3. **Cheap and simple to run.** A modular monolith on a single VPS with Docker at first. No microservices, no Kubernetes. Leave room to scale later.
4. **Mobile-friendly API.** Small payloads, cursor pagination, image thumbnails, and endpoints that behave well on flaky networks (idempotent retries, sync-since-cursor).
5. **Every phase ships something usable and tested.**

---

## 3. Tech stack (decided)

| Area | Choice |
|---|---|
| Language | Python 3.12 |
| API | FastAPI, Uvicorn (Gunicorn with Uvicorn workers in production) |
| Database | PostgreSQL 16 with PostGIS 3 and `pg_trgm` |
| ORM and migrations | SQLAlchemy 2.x (async) with asyncpg, GeoAlchemy2, Alembic |
| Validation and settings | Pydantic v2, pydantic-settings |
| Location indexing | H3 via `h3` (h3-py v4), computed in Python |
| Cache, rate limits, OTPs, pub/sub, queue | Redis |
| Background jobs and schedules | arq |
| Object storage | S3-compatible (Cloudflare R2 or AWS S3) with presigned uploads; Pillow for image processing |
| Push notifications | Firebase Cloud Messaging via `firebase-admin` |
| SMS / phone verification | Africa's Talking |
| Payments (Phase 9) | M-Pesa Daraja API (STK Push) |
| Phone numbers | `phonenumbers` |
| Auth tokens | PyJWT |
| Tooling | uv, ruff, mypy, pytest, pytest-asyncio, httpx |
| Local dev | Docker Compose (api, worker, postgres/postgis, redis) |
| CI | GitHub Actions |

Ask before adding any dependency not listed here, and say why it's needed.

---

## 4. How to work

- **Build phase by phase, in order.** At the start of each phase, write a short plan in `PROGRESS.md`: models, endpoints, migrations, workers, and open questions.
- **Questions.** If an open question could cause significant rework, ask me before building. Otherwise make a reasonable choice and record it in `docs/DECISIONS.md` (one short entry per decision: context, choice, alternatives considered).
- **End of each phase:** all tests pass, migrations apply cleanly on an empty database, OpenAPI docs are current, and `PROGRESS.md` says what was built, what was deferred, and how to try it. Then **stop and give me a summary.** Do not start the next phase until I say so.
- **Don't build later-phase features early,** but design the data model so later phases don't need painful migrations. For example, conversations should have a nullable reference for a listing before the marketplace exists.
- Make small, focused commits with conventional commit messages.
- Never commit secrets. Keep a complete `.env.example`.
- If something in this brief looks wrong, risky, or contradictory, tell me instead of quietly working around it.

---

## 5. Architecture and conventions

### Project layout

```
app/
  main.py
  core/          config, db session, security, errors, pagination, rate limiting, logging
  modules/
    auth/  users/  location/  media/  feed/  messaging/  notifications/
    groups/  marketplace/  services/  events/  moderation/  payments/
      models.py  schemas.py  service.py  router.py
  workers/       arq settings and tasks
migrations/      Alembic
tests/
docs/            ARCHITECTURE.md, DECISIONS.md, DATA_MAP.md
```

- Keep routers thin. Business logic lives in `service.py`.
- Modules use each other through service functions, never through each other's routers.
- Shared rules (blocking checks, location privacy, rate limits) live in one place and are reused everywhere.

### API conventions

- All routes under `/v1`. JSON in `snake_case`.
- UUID primary keys.
- Cursor-based pagination for every list: `{ "items": [...], "next_cursor": "..." | null }`.
- One error envelope everywhere: `{ "error": { "code": "...", "message": "...", "details": {...} } }`.
- Timestamps stored and returned in UTC (ISO 8601). Use `Africa/Nairobi` only for user-facing scheduling such as event times, reminders, and "this weekend".
- Money as integers in KES (whole shillings, matching M-Pesa).
- Phone numbers normalised to E.164 (`+2547…`, `+2541…`). Accept `07…`, `01…`, `254…`, and `+254…` as input.
- Support an `Idempotency-Key` header on create endpoints clients may retry (posts, listings, messages, payments).
- User content is soft-deleted (moderation needs evidence) and purged later by a retention job.
- Logs are structured JSON with a request ID. Redact phone numbers, OTPs, tokens, and coordinates.

---

## 6. Location, radius and privacy — the core system

### 6.1 Storing location

- The client sends latitude and longitude. The server **immediately** converts them to an H3 resolution-9 cell (a few hundred metres across) and stores the cell ID plus the cell's centroid as a PostGIS `geography(Point, 4326)`. **Raw coordinates are never persisted and never logged.**
- Content (posts, stories, listings, service posts, public groups, events) stores its own cell and centroid, taken from the author's location at posting time or, for events, the venue.
- Store H3 IDs as `BIGINT` with B-tree indexes; store coarser parent cells (res 7 and 8) where queries need them. Put GiST indexes on all geography columns.
- Rate-limit location updates and flag implausible jumps (likely spoofing).

### 6.2 The adaptive radius

- **Unit of computation:** radius is computed per H3 resolution-8 cell, not per user, so it can be cached and shared.
- **Active user:** opened the app in the last 14 days (configurable).
- **Density data:** a worker refreshes a `cell_activity` table (res-8 cell → active user count) every 15 minutes.
- **Algorithm:** starting from the viewer's res-8 cell, expand H3 rings k = 0, 1, 2, … and sum active-user counts until the total reaches the target or the maximum is hit. Convert the ring distance to kilometres using real H3 cell distances, not a hardcoded constant. Clamp to the feature's min and max.
- **Caching and stability:** cache per `(feature, cell)` in Redis with a TTL. Apply hysteresis: only replace a cached radius if the new value differs by more than 20%, so feeds don't jump around.
- **Querying content:** use `ST_DWithin(content.geog, viewer_centroid, radius_m)` with the GiST index.
- **Visibility to the user:** `GET /v1/me/radius` returns the user's current radius per feature, rounded to 0.5 km, so the app can show "Your radius: 3 km".

Starting values (all in config, to be tuned once real data exists):

| Feature | Rule | Min | Max | User distance filter |
|---|---|---|---|---|
| Feed and stories | target 500 active users | 1 km | 15 km | none |
| Group discovery | 2 × feed radius | 2 km | 25 km | up to 25 km |
| Marketplace | 2 × feed radius | 2 km | 30 km | up to 50 km |
| Jobs and services | 3 × feed radius | 3 km | 40 km | up to 50 km |
| Events | 2 × feed radius | 2 km | 30 km | up to 50 km |

### 6.3 Post reach

Because each viewer has their own radius, a post's audience is not simply the poster's radius. When someone creates a post or listing, return an estimated audience ("visible to about 340 neighbours"), computed by summing active users in the cells whose radius covers the post's location. An approximation is fine; cache it.

### 6.4 Privacy rules (non-negotiable)

1. **No API response ever includes another user's coordinates, centroid, or fine-grained cell ID.** Enforce this at the schema layer: public response models have no such fields. Add a test that scans all public response schemas and fails if any location field appears.
2. **Area labels with k-anonymity.** A user's displayed location is an area label. Snap their res-9 cell up to the finest parent (res 8, then 7, then 6) that contains at least K registered users (K = 10, configurable). Show the name of the admin-defined area containing it (an `areas` table of seeded polygons, such as estates in Nyeri), or a generic fallback like "near Nyeri town".
3. **Distance buckets, not exact distances.** Show distances to other users or their content as buckets ("under 1 km", "1–3 km", "3–10 km", "10+ km"), computed from coarse cells. This prevents trilateration attacks, where someone spoofs their location and measures distances.
4. **Images:** strip all EXIF metadata (including GPS) before storing the public version. Discard the original.
5. **Events:** private events reveal the exact venue only to accepted RSVPs. Everyone else sees the area label.
6. **Marketplace:** listings show an approximate pickup area. The exact pickup point is arranged in chat.

---

## 7. Kenya-specific requirements and compliance

- **Data Protection Act, 2019:**
  - Record consents with timestamps and document versions: terms, privacy policy, location use, marketing.
  - Data minimisation: collect only what a feature needs.
  - Right of access (export my data as JSON), rectification, and deletion (30-day grace period, then purge across all modules, keeping only what the law requires, such as payment records).
  - An audit log ready for breach investigation.
  - Maintain `docs/DATA_MAP.md`: what personal data each table holds, why, and how long it's kept.
- **Minimum age 18.** Users confirm this at signup. This avoids the Act's parental-consent requirements for children.
- **Providers behind interfaces:** `SmsProvider` (Africa's Talking, plus a fake/console provider for dev and tests), `PushProvider` (FCM, plus a fake), `StorageProvider`, and later `PaymentProvider` (M-Pesa Daraja). Tests never call real external services.
- **Low bandwidth:** WebP thumbnails at a few sizes, lean list responses (no heavy nested objects), and ETag support on rarely changing resources where it's cheap.
- **Language:** the API returns codes and data; the app handles display text. Scam and abuse heuristics must cover English and Swahili.

---

## 8. Phases

### Phase 0 — Foundation

- Scaffold the project layout above with uv and `pyproject.toml`.
- Docker Compose with `api`, `worker`, `postgres` (PostGIS image, with `postgis` and `pg_trgm` enabled), and `redis`.
- Settings via pydantic-settings; `.env.example`.
- Async SQLAlchemy engine and sessions; Alembic configured for async and GeoAlchemy2.
- Error envelope, request-ID middleware, structured logging with redaction.
- Redis-backed rate-limiting utility (per user and per IP).
- Cursor pagination helper.
- `/health` and `/ready` endpoints.
- Pytest against a real PostGIS database with per-test transaction isolation and data factories.
- GitHub Actions: ruff, mypy, and pytest with PostGIS and Redis service containers.

**Done when:** `docker compose up` gives a running API with docs at `/docs`, a sample test hits the database, and CI is green.

### Phase 1 — Accounts, consent and location

- **Phone OTP:** request and verify endpoints. 6-digit codes, stored hashed in Redis, 5-minute expiry, max 5 attempts, 60-second resend cooldown, daily caps per number and per IP.
- **Tokens:** short-lived JWT access tokens (about 15 minutes) and rotating refresh tokens (about 30 days), stored hashed per device session. Logout, logout-all, and refresh-reuse detection that revokes the whole token family.
- **Signup:** display name, 18+ confirmation, consent records.
- **Profiles:** get and update my profile; public profile showing display name, area label, member-since, and placeholders for badges and rating.
- **Location:** set location, H3 conversion, discard raw coordinates, basic spoof detection.
- **Radius engine:** `cell_activity` worker, radius computation with caching and hysteresis, `GET /v1/me/radius`.
- **Areas:** `areas` table, area labels with k-anonymity, and an admin seed script for area polygons.
- **Blocking:** block, unblock, and list blocks, through one central check used by every later module.
- **Data rights:** account deletion request and data export (basic version, extended as modules are added).
- **Open question for me before you build it:** is the feed anchored to the user's home location, their current location, or both? (Suggested default: home location, with an optional "explore here" mode using current location.)

**Done when:** a user can sign up with OTP (fake provider), set a location, and see their radius and area label. Tests with synthetic users around Nyeri prove that the radius shrinks in dense areas, widens in sparse ones, respects the limits, and doesn't flicker. The privacy schema test passes.

### Phase 2 — Media and neighbourhood feed

- **Media:** presigned upload URLs; the client uploads directly to storage. A worker validates type and size, strips EXIF, and generates WebP sizes (for example 320, 720, 1280 px). Only processed versions are ever served.
- **Avatars** on profiles.
- **Posts:** text (with a length limit), up to 6 images, and a category from a configurable list (for example general, question, recommendation, lost & found, alert).
- **Feed:** `GET /v1/feed` returns posts within the viewer's feed radius, newest first, cursor-paginated, excluding blocked users and removed content. Structure it so ranking can be added later without changing the API.
- **Stories:** image or text, expiring after 24 hours (worker cleanup), with a viewer list for the author.
- **Comments** (one level of replies) and **reactions**.
- **Reach estimate** returned on post creation.
- **Reporting:** report a post, comment, story, or user with a reason, into a moderation queue. Auto-hide content after N reports from distinct trusted users, pending review (configurable).
- Tighter rate limits for new accounts.

**Done when:** two test users in the same area see each other's posts, a user far away doesn't, blocked users' content never appears, and uploaded images come back without EXIF.

### Phase 3 — Messaging and notifications

- **Conversations:** direct, group, and listing (buyer–seller, with a nullable listing reference for Phase 5).
- **Message requests:** a first message from someone with no accepted conversation lands in the recipient's requests. The sender can send only one message until it's accepted. The recipient can accept, decline, or block and report.
- **Messages:** text, images (via media), reply-to, and soft delete ("message deleted").
- **Realtime:** persist first, then publish through Redis pub/sub to WebSocket connections at `/v1/ws` (authenticated with the access token). This must work with multiple API workers. Reconnecting clients sync with "messages since cursor". Read state stored per member as last-read message.
- **Group chats** with a configurable member limit and admins.
- **Notifications:** register FCM device tokens, per-type preferences, an in-app notifications list, and push sent from the worker. Collapse bursts so users aren't spammed. Add notifications for feed activity (comments, replies, reactions).

**Done when:** two clients exchange messages in realtime across two API workers, offline users get a push, message requests behave as described, and blocked users cannot message each other.

### Phase 4 — Groups

- **Public groups** have a location and are discoverable within the group-discovery radius, with name and category search (`pg_trgm`).
- **Private groups** are invite-only (expiring, revocable invite links, or direct invites) and never appear in discovery.
- **Joining:** open join, or join requests that need approval.
- **Roles:** owner, admin, moderator, member. **Admin tools:** approve and remove members, ban, mute, pin posts, require post approval, delete posts, set group rules, and transfer ownership.
- **Group posts** reuse the post model with a group scope; each group can have a linked group chat.
- **Categories:** estate, church, chama, students, sports, business, other (configurable).

**Done when:** permission tests prove that non-members can't read private groups, members can't use admin tools, and banned users can't rejoin.

### Phase 5 — Marketplace

- **Listings:** title, description, price in KES (or free / negotiable), category, condition, up to 8 photos, approximate pickup area, status (available, reserved, sold), and expiry after 30 days with renewal.
- **Browse and search** within the marketplace radius, with the user's distance filter. Full-text search plus filters (category, price range, condition) and sorting (newest, nearest bucket, price).
- **Saved listings**; "chat with seller" opens a listing conversation; mark as sold.
- **Anti-abuse:** daily listing limits for new accounts, a configurable prohibited-items list, and basic duplicate detection.
- Design for seller reviews (they arrive in Phase 6).

**Done when:** a user can list an item, a nearby buyer finds it by search, chats with the seller, and the seller marks it sold.

### Phase 6 — Jobs, services and reviews

- **Three post types:** offering a service (provider profile with categories, description, rate info, availability, service area), needing a service, and hiring (role, pay, job type).
- **Seeded categories** (configurable): plumbing, electrical, mama fua/laundry, house help, tutoring, boda boda/delivery, mechanics, salon/barber, tailoring, construction/fundi, catering, photography, phone/IT repair, cleaning, other.
- **Search** within the services radius.
- **Reviews:** 1–5 stars plus text, allowed only when the two users had an accepted conversation tied to that service, job, or listing. One review per pair per context. Providers can reply; reviews can be reported. Cache aggregate ratings.
- **Job-scam protections:**
  - New or unverified accounts can't post "hiring" until they meet a trust threshold (account age and phone verification now; the verified badge from Phase 8 later).
  - Heuristic flagging for fee demands in English and Swahili (for example "registration fee", "ada ya usajili", "pay to apply", requests to send money to secure a job). Flagged posts are held for review.
  - A safety notice attached to every job conversation ("Never pay to get a job").
  - "Job scam" reports are prioritised in the moderation queue.

**Done when:** a provider can be found, contacted, and reviewed; reviews without a qualifying conversation are rejected; a test scam post is held for review.

### Phase 7 — Events

- **Event fields:** title, description, type (party, church, sports, harambee, clean-up, meetup, other), start and end in `Africa/Nairobi`, venue, cover image, optional capacity, and organiser (a user or a group).
- **Visibility:** public (discoverable in the events radius), group-only, or private (invite-only).
- **Location privacy:** private events reveal the exact venue only to accepted RSVPs.
- **RSVPs:** going / interested / not going, an approval mode for private events, and attendee-list visibility settings.
- **Reminders** from the worker (24 hours and 1 hour before, configurable) for users who are going.
- **Changes and cancellations** notify everyone who RSVP'd.
- **Browse** upcoming events within the radius, filtered by type and date ("today", "this weekend").

**Done when:** reminders fire at the right local times in tests, and private-event venues are hidden from non-attendees.

### Phase 8 — Trust, safety and launch hardening

- **Verified-neighbour badge,** computed by a worker. Starting rule (configurable): phone verified, account at least 14 days old, location consistent with the home area on at least 10 of the last 14 active days, and no upheld reports in 90 days; **or** vouched for by 2 verified neighbours in the same area. Badges can be revoked.
- **Moderation API** for an admin console: a prioritised report queue (scams and safety first), actions (remove, restore, warn, suspend, ban), user history, and an audit log of every moderator action. Admin and moderator roles are separate from normal user auth.
- **Restrictions** for suspicious accounts, and ban lists for phone numbers and devices.
- **Data protection completeness:** export and deletion cover every module; retention jobs run; `DATA_MAP.md` is complete.
- **Performance:** seed realistic data (for example 50,000 users around Nyeri and 500,000 posts). Check that p95 latency for feed and marketplace queries is under 200 ms, `EXPLAIN` the key queries, and fix indexes.
- **Operations:** production Dockerfile, deployment notes for a single VPS behind a reverse proxy (Caddy or Nginx), automated Postgres backups with a tested restore, error tracking, metrics, log retention, and secrets handling.
- **Security review:** go through the OWASP API Security Top 10, and add authorisation tests (nobody can read others' DMs, private groups, private events, or precise locations).

**Done when:** the backend is ready for the Nyeri launch.

### Phase 9 — Monetisation I: featured providers and promoted listings

- **M-Pesa Daraja STK Push** behind `PaymentProvider`. Callbacks are verified, idempotent, and reconciled with a transaction status query. The payments table is the source of truth; never trust what the client says.
- **Products:** featured service provider (N days, boosted in services search, marked "Featured") and promoted listing (N days, boosted in the marketplace, marked "Promoted").
- **Ranking:** boosted items appear in fixed slots and still respect the radius (no Nairobi promotions shown in Nyeri).
- Configurable pricing, receipts, and admin refunds.

### Phase 10 — Monetisation II and expansion (outline; we'll refine it later)

- Business profiles (verified businesses, opening hours, catalogue, basic analytics).
- Marketplace fees if in-app payments for items are introduced.
- Event ticketing (paid tickets via M-Pesa, QR check-in).
- Expansion tooling: seeding areas for Karatina, Othaya, Nanyuki, and Nairobi.

---

## 9. Testing standards

- Every endpoint: happy path, unauthenticated, forbidden, and validation errors.
- Radius and location: unit and integration tests with synthetic users at known coordinates (approximately Nyeri town -0.42, 36.95 and Karatina -0.48, 37.13), covering dense, sparse, and boundary cases.
- The privacy schema test from section 6.4 runs in CI.
- A blocking test in every module: a blocked user's content and messages never appear.
- Worker tasks tested by calling the task functions directly.
- Aim for meaningful coverage of service logic (around 80%) rather than chasing a number.

---

## 10. Definition of done for every phase

- [ ] All tests pass locally and in CI.
- [ ] Migrations apply cleanly on an empty database and downgrade without errors.
- [ ] OpenAPI docs reflect every new endpoint, with examples.
- [ ] New personal data is recorded in `docs/DATA_MAP.md` and covered by export and deletion.
- [ ] Blocking, rate limiting, and privacy rules apply to everything new.
- [ ] `PROGRESS.md` and `docs/DECISIONS.md` are updated.
- [ ] You have stopped and summarised the phase for me.

---

## 11. Addenda agreed during the build

### Feed anchoring (answered before Phase 1)

The feed is anchored to **home**, with "explore here" as an optional mode, plus
five supporting rules:

1. Posts always go to your home area, even while exploring. That stops people
   dropping posts or listings into neighbourhoods they don't live in.
2. Explore mode lives only in the current session. The current location sits in
   Redis for about 2 hours and is never saved to the database. Other people only
   ever see your home area.
3. Visitors are labelled. If you comment while exploring, your comment shows your
   home area (for example "Karatina"), so locals know you're visiting.
4. Changing home is allowed but limited to about once every 14 days, so spammers
   can't hop around. DeKUT students moving between campus and home over the
   holidays can still switch.
5. The verified badge uses only a daily yes/no for "was the user in their home
   area today", never the location itself.

Three sub-questions arising from these rules were answered with the recommended
values: exploring users do not count toward density, changing home resets the
badge's consistency window, and explore days are excluded from the badge count
rather than counted as absence. Recorded in full as decision 1.1.
