# Data map

What personal data this system holds, why it holds it, and how long it keeps it.
Maintained for the **Data Protection Act, 2019 (Kenya)**, which requires us to be
able to answer those three questions for every field, and to honour access,
rectification, and erasure requests across all of it.

**Rule for every phase:** a phase that introduces personal data updates this file
and extends the export and deletion routines in the same change. A field that is
not in this table cannot be exported or erased, because nobody knows it exists.

- **Data controller:** MyRadius (registration with the Office of the Data
  Protection Commissioner to be completed before the Nyeri launch — Phase 8).
- **Lawful basis:** consent for optional processing (location use, marketing),
  and performance of a contract for the data a requested feature cannot work
  without. Consents are recorded with a timestamp and the document version.
- **Minimum age:** 18, confirmed at signup. This keeps the Act's parental-consent
  requirements for children out of scope.

---

## Phase 0 — Foundation

No database tables exist yet, so no personal data is **stored**. Two things are
nonetheless worth recording, because both are personal data under the Act and
both exist from the first deployed request.

### Application logs

| Field | Why | Retention | Notes |
| --- | --- | --- | --- |
| `client_ip` | Abuse investigation, rate-limit forensics, incident response | 30 days (Phase 8 sets this in the log pipeline) | An IP address is personal data. It is the only identifier in Phase 0 logs. |
| `request_id` | Correlating a user's report with server logs | 30 days | Random per request; identifies nothing by itself. |
| `method`, `path`, `status_code`, `duration_ms` | Operations and performance | 30 days | Paths may contain resource UUIDs. Query strings are **not** logged, because location endpoints carry coordinates in them. |

**What is kept out of logs, by filter rather than by convention:** phone numbers,
OTP codes, tokens, passwords, coordinates, centroids, and H3 cell IDs. Enforced
by `RedactionFilter` on the root logger and tested in `tests/test_logging.py`.
See decision 0.2 for the one accepted gap.

### Redis

| Key pattern | Contents | Why | Retention |
| --- | --- | --- | --- |
| `rl:{bucket}:{identity}` | A request counter. `identity` is `ip:<address>` or `u:<user id>`. | Rate limiting | Expires with its window: seconds to 24 hours |

Nothing in Redis is a source of truth, and everything in it expires.

---

## Phase 1 — Accounts, consent and location

### `users`

| Field | Why | Retention | Notes |
| --- | --- | --- | --- |
| `phone_number` | The account identifier. Sign-in has to find an account from the number alone; there is no second factor to look it up by. | Until purge, then **null** | E.164, uniquely indexed, stored in the clear because it must be searchable. Redacted from logs by pattern. Null means the account was purged. |
| `phone_verified_at` | Proof the number was controlled at signup; feeds the verified-neighbour rule | Until purge | |
| `display_name` | Shown to neighbours | Until purge, then "Deleted user" | Free text; the user chooses how identifiable to be. |
| `status` | Account state: active, suspended, banned, deletion_pending | Until purge | |
| `adult_confirmed_at` | Evidence of the 18+ declaration, and when it was made | Until purge | A timestamp rather than a boolean: the Act asks *when* a declaration was made. |
| `last_active_at` | Defines "active" for the density counts the radius is built on | Until purge | Updated on authenticated requests. |
| `avatar_media_id` | Profile picture (Phase 2) | Until purge | Null throughout Phase 1. |
| `home_cell_res9/8/7/6` | The approximate home location, as H3 cells | Current value only | **No history.** A change overwrites. Res 9 spans a few hundred metres; the parents drive density and k-anonymity. |
| `home_centroid` | The cell's centre, for PostGIS proximity queries | Current value only | The centre of the cell, **never** the coordinates the client sent. |
| `home_area_id` | Which seeded area the home falls in | Current value only | Null when outside every seeded area, which is normal before a town is seeded. |
| `home_set_at`, `home_changed_at` | Enforce the change cooldown; input to spoof detection | Current value only | |
| `home_presence_mask` | The verified-neighbour badge rule | 14 days, rolling | **A 14-bit integer: one yes/no per day for "was in the home area".** The entire location history the system keeps. Never the positions it was derived from. |
| `home_presence_updated_on` | Which day the mask was last rolled | Current value only | Needed so a multi-day gap shifts by the right number of bits. |
| `is_verified_neighbour`, `verified_neighbour_at` | Trust badge (computed Phase 8) | Until purge | |
| `rating_average`, `rating_count` | Provider reputation (Phase 6) | Until purge | Null/zero throughout Phase 1. |
| `deletion_requested_at` | Start of the erasure grace period | Cleared on cancel; drives the purge | |

**What is deliberately absent:** there is no location-history table, no
coordinate column, and no record of where a user has been. Only the current home
cell exists.

### `consents`

| Field | Why | Retention |
| --- | --- | --- |
| `user_id`, `consent_type`, `granted`, `document_version`, `created_at` | The Act requires consent to be recorded with its timestamp and the document version agreed to | 2 years after the account is purged |

Append-only. A withdrawal is a new row, not an update, because the question the
Act asks is "what did this person agree to, and when?" — and a withdrawal is
itself a fact worth keeping.

### `blocks`

| Field | Why | Retention |
| --- | --- | --- |
| `blocker_id`, `blocked_id`, `created_at` | Safety. One row per block, applied in both directions by `users.service.blocked_user_ids` | Until either account is purged |

### `device_sessions`

| Field | Why | Retention |
| --- | --- | --- |
| `refresh_token_hash` | Finding the session a token belongs to | Until expiry + 30 days |
| `family_id`, `replaced_by_id` | Refresh reuse detection and tracing | Until expiry + 30 days |
| `device_name`, `device_platform` | So a user recognises their own devices on the sessions screen | Until expiry + 30 days |
| `expires_at`, `last_used_at`, `revoked_at`, `revoked_reason` | Session lifecycle and security forensics | Until expiry + 30 days |

The refresh token itself is never stored — only an HMAC of it — so a database
leak does not hand over working credentials. Device names are client-supplied,
displayed only to the account's own owner, and never used for authorisation.

### `areas`

Seeded polygons with names. **Not personal data**: no area is created by or about
a user. Listed here because it is what makes location labels possible.

### `cell_activity`

Aggregate counts per H3 cell: active and registered users. **Not personal data** —
no row identifies anyone, and a cell with fewer than K users is exactly what
causes a label to be withheld.

### `audit_events`

| Field | Why | Retention |
| --- | --- | --- |
| `actor_type`, `actor_id`, `action`, `target_type`, `target_id`, `context`, `request_id`, `created_at` | The Act's requirement for an audit trail ready for breach investigation | 2 years |

`actor_id` is a bare UUID, not a foreign key, **deliberately**: the row outlives
the account it concerns, because an audit log that disappears with the account is
useless for the investigations it exists for. It therefore **survives a purge**,
and the deletion flow should say so.

`context` must never hold coordinates, cell IDs, phone numbers, or tokens.
`moderation.service.record_audit_event` enforces this with the same key rule the
log redaction filter uses, rather than trusting callers.

### Redis

| Key pattern | Contents | Why | Retention |
| --- | --- | --- | --- |
| `otp:<hash>` | Hashed one-time code and its attempt counter | Phone verification | 5 minutes |
| `otp:cooldown:<hash>` | Resend cooldown marker | Cost control: each send is a real SMS | 60 seconds |
| `signup:<hash>` | Phone number awaiting signup completion | Single-use ticket between verify and signup | 15 minutes |
| `explore:<user id>` | **An explore-mode location, as cells** | Browsing elsewhere temporarily | ~2 hours |
| `arealabel:<cell>` | A resolved area label | Avoids recomputing k-anonymity per profile render | 1 hour |
| `radius:feed:<cell>` | A cached feed radius | Stability and cost | 15 minutes |
| `rl:<bucket>:<identity>` | Rate-limit counters | Abuse and cost control | Window length |

Two things to note. OTP keys are keyed by a **hash** of the phone number, not the
number, because Redis keys appear in slow logs and `MONITOR` output. And the
explore location is the most sensitive item in the system — where somebody is
right now — which is exactly why it lives only here, expires by itself, and is
never written to PostgreSQL. It is not in the data export, because there is
nothing at rest to export.

---

## Data subject rights — implementation status

| Right | Status | Where |
| --- | --- | --- |
| Access (export as JSON) | **Implemented** (Phase 1 scope) | `GET /v1/me/export`; extended by each later phase |
| Rectification | **Implemented** | `PATCH /v1/me` |
| Erasure | **Implemented** | `POST /v1/me/deletion-request`, 30-day grace, then the `run_retention` worker purges |
| Restriction of processing | Not started | Phase 8, account restrictions (`UserStatus.suspended` exists) |
| Objection to marketing | **Implemented** | `PUT /v1/me/consents`, marketing consent is withdrawable |
| Data portability | **Implemented** | Covered by the JSON export |
| Right to be informed | Partial | Consent records capture document versions; the policy documents themselves are the client's |

**Rule for later phases:** the export and the purge both have to grow with the
data. `users.service.export_user_data` and `users.service.purge_user` are the two
functions a phase introducing personal data must extend, in the same change that
introduces it.

## Planned retention, by phase

Recorded here now so that retention is designed in rather than retrofitted. Each
becomes a worker job in the phase that owns the data.

| Data | Planned retention |
| --- | --- |
| Account, while active | Until deletion is requested |
| Deleted account | Purged 30 days after the request, except where law requires otherwise |
| Soft-deleted user content | 90 days, then purged — kept because moderation needs the evidence |
| Stories | 24 hours, then purged |
| Home location | Only the current H3 cell and its centroid. No history: a change overwrites. |
| Explore-mode location | **Redis only, ~2 hour TTL. Never written to PostgreSQL.** Not in the export, because it is never at rest. |
| Home-presence history | A 14-bit rolling mask of daily yes/no answers to "was this user in their home area". Not the locations it came from. Self-purging. |
| Moderation reports and actions | 2 years, as the record of why an account was acted against |
| Audit log | 2 years |
| Payment records | 7 years, as tax law requires. **Survives account deletion**, and the deletion flow must say so. |
| Application logs | 30 days |

---
