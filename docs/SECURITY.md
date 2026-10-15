# Security review

The API reviewed against the **OWASP API Security Top 10 (2023)**, at the end of
Phase 8. For each risk: the controls in place, the tests that hold them in place, and
what is still open. Open items are listed together at the end.

Two properties cut across everything below:

- **Location.** No raw coordinate is stored or logged. No response carries a
  coordinate, a cell or an exact distance.
- **Staff are separate.** Staff are not users, and the moderation API refuses user
  tokens.

## API1 — Broken object level authorization

**Controls**
- Every read goes through a module's `access` functions: `groups.access`,
  `events.access`, `marketplace.access`, `services.access`, and conversation
  membership in messaging.
- Those functions answer **404, not 403**, for anything the caller may not see, so a
  refusal doesn't confirm the object exists.
- Reporting resolves the target through the same rules as reading. The report
  endpoint can't be used to probe for hidden or blocked content.
- Blocks apply in both directions, everywhere.

**Tests**
- `tests/test_authorization.py`: an outsider is refused on all 55 routes on a direct
  conversation, its messages, a private group and its posts, and a private event.
  None of their secret text appears in any response or list. A meta-test fails if a
  new route on those resources isn't added to the matrix.
- Per-module suites:
  - `test_group_permissions.py`: the role × endpoint matrix.
  - `test_events.py`: private venues.
  - `test_messaging_api.py` and `test_message_lifecycle.py`.

## API2 — Broken authentication

**Users**
- **Codes:** six digits over SMS, held only as an HMAC, five attempts, five minutes,
  single use. Daily caps per number and per IP, which fail closed.
- **Access tokens:** JWTs, 15 minutes, algorithm pinned to the configured one.
- **Refresh tokens:** 32 random bytes, stored as an HMAC, rotated on every use. A
  replayed refresh token revokes its whole session family.
- **Revocation:** every request checks the session row, so signing out takes effect
  at once.
- **Request answers:** a code request answers the same whether or not the number has
  an account.

**Staff**
- **Sign-in:** email, a scrypt password (N=2^15) and a TOTP code, all three in one
  request, with one answer for every failure. A wrong email costs the same time as a
  wrong password.
- **Codes:** each is accepted once (replay protection), with one step of clock drift
  either way.
- **Lockout:** after `STAFF_MAX_FAILED_LOGINS` failures, the account locks for
  `STAFF_LOCKOUT_MINUTES`.
- **Tokens:** a separate token type and audience; 8-hour sessions with no refresh
  token; revocable.
- **Bootstrap:** the first admin comes from a script that refuses once an admin
  exists.

**Configuration:** the app refuses to boot in staging or production with:
- the development `SECRET_KEY`, or one under 32 bytes;
- `DEBUG` on;
- console SMS, in-memory storage, or the fake push provider.

**Tests**
- `test_auth_api.py`, `test_phone_and_security.py`, `test_config.py`.
- `test_staff.py`:
  - the RFC 6238 test vector, drift and replay;
  - one answer for every failure, lockout and audit;
  - token separation in both directions.

## API3 — Broken object property level authorization

**Controls**
- Every response is an explicit Pydantic model. No ORM object is serialised directly.
- Request models declare only the fields a client may set, and unknown fields are
  ignored, so there is no mass assignment: status, roles, ratings, counts and badges
  can't be set by a client.
- Phone numbers appear only in the owner's own export.
- Venue details of a private event appear only to people going.
- Moderators see names and standing, never phone numbers or cells.

**Tests**
- `test_privacy_schemas.py`: walks every response schema for location vocabulary.
- `test_authorization.py::test_no_response_carries_a_location`: walks real responses.

## API4 — Unrestricted resource consumption

**Controls**
- **Rate limits** per account and per IP, in Redis, on every write. New and restricted
  accounts get tighter tiers.
- **Pagination:** cursor-based, capped at `PAGINATION_MAX_LIMIT`.
- **Database:** a 10-second statement timeout on every connection.
- **Uploads:** at most 12 MB and 50 megapixels per image. Images go straight to
  object storage by presigned URL, and processing happens in the worker.
- **Bodies:** Caddy caps request bodies at 1 MB.
- **Sockets:** WebSocket authentication must complete within 10 seconds, and
  heartbeats are bounded.
- **Volume:** the feed and marketplace are measured at p95 168 ms with 50,000 users
  and 500,000 posts (Phase 8 benchmark).

**Tests:** `test_ratelimit.py` (including concurrent requests) and the new-account
limit tests in each module.

## API5 — Broken function level authorization

**Controls**
- **Admin API:** `/v1/admin` accepts staff tokens only. Within it, bans, the ban
  lists, the audit log and staff management are admin-only.
- **Group actions:** checked against one role table (`groups.permissions`).
- **Event management:** organisers and group admins only.
- **Account state:** suspended, deletion-pending and restricted accounts are refused
  by `ActiveUser`, the rate-limit tiers and the restriction checks.

**Tests**
- `test_staff.py` and `test_moderation_console.py::test_only_admins_ban`.
- `test_group_permissions.py`.

## API6 — Unrestricted access to sensitive business flows

**Controls**

| Flow | Control |
| --- | --- |
| SMS pumping | Per-number and per-IP daily caps, failing closed. A banned number gets the same answer and no SMS. |
| Spam | New-account tiers on posts, comments, stories, listings, services, events, message requests and invites. |
| Message requests | One message until the other side replies. |
| Job scams | Fee-demand phrases hold a job post for review, and posting a job needs an account at least `HIRING_MIN_ACCOUNT_AGE_DAYS` old. |
| Reports | Auto-hide counts distinct trusted reporters only, so one person can't silence another. |
| Reports against a person | Reports can restrict a person, never hide or suspend them. |
| The badge | Needs real presence over 14 days, or vouches from verified neighbours in the same area. |

**Tests:** `test_reports.py`, `test_moderation_console.py` (auto-restriction and
untrusted reports), `test_services.py`, `test_badge.py`.

## API7 — Server side request forgery

**Controls**
- The server never fetches a URL a client supplied.
- Images are uploaded by the client to a presigned URL the server generated, and the
  worker reads them back from our own bucket by our own key.
- Outbound calls go only to fixed endpoints: Africa's Talking, FCM and the object
  store.

**Tests:** none needed beyond review. There is no fetch-by-URL code path to test.

## API8 — Security misconfiguration

**Controls**
- **Production guards** at boot (see API2).
- **CORS:** off by default.
- **Caddy:**
  - sets HSTS, `nosniff`, `DENY` framing and `no-referrer`;
  - overwrites `X-Forwarded-For` rather than appending to it, so the client IP the
    rate limiter sees can't be forged;
  - refuses `/metrics` from outside.
- **Network:** only Caddy publishes ports.
- **Runtime image:** runs as a non-root user.
- **Secrets:** come from a mode-600 env file and a read-only mount, never the image.
- **Errors:** an error envelope that never includes stack traces.
- **Sentry:** strips headers, query strings, bodies, locals and IP addresses.
- **Logs:** redacted at the root logger. Gunicorn's own access log, which wasn't
  redacted, was removed.

**Tests**
- `test_config.py`, `test_errors.py`, `test_logging.py`.
- `test_observability.py`: Sentry scrubbing, and metrics behind their token.

## API9 — Improper inventory management

**Controls**
- One versioned prefix, `/v1`.
- OpenAPI is generated from the code, and every operation must have a summary, a
  description and a documented tag.
- Staff routes sit under `/v1/admin`, in their own tags.
- There are no undocumented or legacy routes.
- Staging and production are separate deployments with separate secrets.

**Tests:** `test_openapi_docs.py`.

## API10 — Unsafe consumption of APIs

**Controls**
- **Africa's Talking:** called with a timeout. A failure is logged, not trusted.
- **FCM:** errors are classified. Tokens FCM reports dead are deleted.
- **Object store:** keys are generated by us, never taken from a response.
- **Images:** decoded with Pillow under a pixel limit, re-encoded, EXIF stripped.

**Tests:** `test_push_provider.py`, `test_images.py`, `test_media_api.py`.

## Open items

None of these blocks launch. Each is recorded so it is a decision rather than an
oversight.

1. **Rate limits fail open when Redis is down.** This applies everywhere except the
   OTP and sign-up routes, which fail closed. Availability is chosen over strictness
   because a Redis blip should not take the app down. Watch Redis health.
2. **TOTP secrets are stored unencrypted.** A database leak alone isn't enough to sign
   in as staff, because the scrypt password is still needed. Encrypting them with a
   key held outside the database is a good later step.
3. **API documentation is public.** `/docs` and `/openapi.json` are open in
   production on purpose: the client team uses them, and the schema contains no
   secrets.
4. **No device attestation.** The installation ID that device bans use is supplied by
   the client. A determined abuser can reinstall to get a new one, and the phone ban
   is the stronger control. Play Integrity and App Attest are possible later.
5. **The development `SECRET_KEY` is 29 bytes.** This is local only, and the app
   refuses it outside local and test.
6. **One-machine deployment.** Backups and the weekly restore test bound the damage
   (`docs/DEPLOYMENT.md` §8).
