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

## Data subject rights — implementation status

| Right | Status | Where |
| --- | --- | --- |
| Access (export as JSON) | Not started | Phase 1 (basic), extended each phase |
| Rectification | Not started | Phase 1, profile update |
| Erasure | Not started | Phase 1 (request + 30-day grace), completed Phase 8 |
| Restriction of processing | Not started | Phase 8, account restrictions |
| Objection to marketing | Not started | Phase 3, notification preferences |
| Data portability | Covered by the JSON export | Phase 1 |

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
