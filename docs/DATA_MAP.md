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

## Phase 2 — Media and neighbourhood feed

### Object storage

| What | Why | Retention | Notes |
| --- | --- | --- | --- |
| Processed WebP variants | Serving images | Until the content is deleted or the account purged | **Re-encoded from decoded pixels, so they carry no EXIF at all** — no GPS, no capture time, no device serial. Keys are random and partitioned by owner. |
| Uploaded originals | Input to processing | **Deleted as soon as processing finishes** | The one copy that still carries EXIF. Rejected uploads are deleted too. |
| Abandoned uploads | — | Swept after 24 hours | Presigned but never confirmed. |

### `media_assets`

| Field | Why | Retention |
| --- | --- | --- |
| `owner_id`, `purpose`, `status` | Ownership and processing state | With the asset |
| `declared_content_type`, `detected_content_type` | Diagnosing rejected uploads | With the asset |
| `original_key` | Where the upload landed | **Nulled once processed**, recording that the EXIF-bearing original is gone |
| `byte_size`, `source_width`, `source_height`, `variants` | Serving | With the asset |
| `rejection_code`, `rejection_reason` | Telling the uploader why | With the asset |

On account purge the stored objects are deleted and the row is kept, marked
`rejected` with code `purged` and no variants: a soft-deleted post still
references it, and nothing servable remains.

### `posts` and `post_images`

| Field | Why | Retention |
| --- | --- | --- |
| `author_id`, `body`, `category` | The post | Soft-deleted on request or purge; purged by the Phase 8 retention job |
| `cell_res9/8/7/6`, `centroid`, `area_id` | Radius filtering and the area label | With the post |
| `status`, `hidden_at` | Moderation state | With the post |
| `reach_estimate`, `comment_count`, `reaction_count` | Display | With the post |

The location is the **author's home cell at posting time** — a cell centre, never a
coordinate — and stays with the post if the author later moves. Authored content
never takes an explore-mode location (decision 1.1, rule 1).

### `stories` and `story_views`

| Field | Why | Retention |
| --- | --- | --- |
| Story: author, kind, text or image, location cells, `expires_at` | The story | **24 hours, then hard-deleted with its image** — unless a report against it is pending |
| `story_views`: `story_id`, `viewer_id`, `created_at` | The author's viewer list | Deleted with the story |

One view row per viewer, never a count of repeat views: the feature promises the
author learns *who* looked, not who keeps coming back. Shown only to the author,
and filtered through blocking.

### `comments` and `reactions`

| Field | Why | Retention |
| --- | --- | --- |
| Comment: `post_id`, `author_id`, `parent_id`, `body`, `status` | The conversation | Soft-deleted; purged with the account |
| `is_visiting` | Marks a comment written while exploring (decision 1.1, rule 3) | With the comment |
| Reaction: `user_id`, target, `kind`, `created_at` | Reactions | Deleted on account purge |

`is_visiting` is a boolean, not a location: it records *that* the author was
elsewhere, never *where*.

### `reports`

| Field | Why | Retention |
| --- | --- | --- |
| `reporter_id`, `target_type`, `target_id`, `reason`, `details`, `priority`, `status` | The moderation queue | 2 years (Phase 8 job) |
| `counted_towards_auto_hide` | Whether the reporter was trusted at the time | With the report |
| `resolved_at`, `resolved_by_id`, `resolution_note` | Moderator outcome (Phase 8) | With the report |

`details` is free text from the reporter and may name people. It is visible only
to moderators. Reports survive the purge of the reported account, for the same
reason the audit log does: they are the record of why somebody was acted against.

### `idempotency_records`

| Field | Why | Retention |
| --- | --- | --- |
| `user_id`, `endpoint`, `key`, `request_fingerprint` | Recognising a retried request | **24 hours** |
| `response_status`, `response_body` | Replaying the original response | **24 hours** |

**Note:** `response_body` is a copy of a create response, so it holds whatever that
response held — a post's body, for instance. It is purged daily by `run_retention`
after 24 hours, which is long enough for any realistic retry and short enough that
it is not a second, shadow copy of anyone's content.

### Data rights in Phase 2

- **Export** now includes the user's posts, comments, stories and reactions —
  soft-deleted ones included and marked, since the right of access is to what is
  held — and a list of their uploads. Upload entries carry no URLs: an export file
  is something people forward, and a working link to every image would outlive its
  purpose.
- **Purge** now soft-deletes the account's posts, comments and stories, deletes its
  reactions and story views, and deletes every stored image it uploaded.

---

## Phase 3 — Messaging and notifications

Private messages are the most sensitive content the product holds. Three rules
apply to all of it:

1. **Nobody outside a conversation can read it.** Every refusal is a 404.
2. **The one exception is a report.** Reporting a message shows it to moderators
   (Phase 8), and only because a member chose to report it.
3. **Nothing private passes through Redis.** Realtime events carry IDs and sequence
   numbers, not text (decision 3.2).

### `conversations`

| Field | Why | Retention |
| --- | --- | --- |
| `kind`, `title`, `state` | What the conversation is; group chat name; request state | Life of the conversation |
| `created_by_id`, `request_sender_id` | Who started it, and who asked first: context for a harassment report | Life of the conversation |
| `direct_key` | One direct conversation per pair (two user IDs) | Life of the conversation |
| `last_seq`, `last_message_at` | Sync and inbox order | Life of the conversation |
| `listing_id`, `group_id` | Phases 5 and 4 | — |

### `conversation_members`

| Field | Why | Retention |
| --- | --- | --- |
| `user_id`, `role`, `status`, `invited_by_id` | Membership and permissions | Life of the conversation; set to `left` on purge |
| `last_read_seq` | Read state: one number, not a row per message | As above |
| `visible_from_seq` | Hides messages from before a member joined | As above |
| `muted` | Push suppression | As above |

### `messages`

| Field | Why | Retention |
| --- | --- | --- |
| `sender_id`, `seq`, `kind`, `reply_to_id` | The conversation's structure | Life of the conversation |
| `body` | The message text | **Cleared on deletion and on account purge**, unless a report is pending |
| `media_id` | An image, through `media_assets` (purpose `message`) | Image objects deleted on message deletion and on purge |
| `deleted_at` | "Message deleted" placeholder | The row stays so numbering holds; hard purge in Phase 8 |

**Read receipts** are `last_read_seq` values. Other members see them only once a
conversation is accepted, so the sender of a request does not learn it was read.

### `device_tokens`

| Field | Why | Retention |
| --- | --- | --- |
| `token` | The FCM address of one installed app | Deleted when FCM reports it dead, when not re-registered for `DEVICE_TOKEN_STALE_DAYS` (90), and on purge |
| `device_session_id` | Pushes go only to live sign-ins | Cascades with the session |
| `platform`, `last_seen_at` | Delivery and staleness | As the token |

Not a credential for this API, but personal data: it addresses a person's phone.

### `notification_preferences`

Per type, `push_enabled`, `in_app_enabled`, and `show_preview` (default **off**:
lock-screen previews on shared phones). A row exists only where the user changed a
default. Deleted on purge.

### `notifications`

| Field | Why | Retention |
| --- | --- | --- |
| `type`, `target_type`, `target_id`, `data` | What happened, as IDs only, never content | **90 days** (`NOTIFICATION_RETENTION_DAYS`) |
| `actor_id`, `actor_ids`, `event_count` | "Kamau and 3 others", up to 50 actors | As above |
| `read_at` | Unread count | As above |

A user's own notifications are deleted on purge. Notifications *about* a purged user
in other people's lists stay, because they belong to the recipient, and they name
"Deleted user".

### Redis

| Key | Holds | Lifetime |
| --- | --- | --- |
| `rt:user:<id>` | Pub/sub channel; events with IDs and sequence numbers only | Not stored: delivered or dropped |
| `presence:<id>` | Connection IDs and expiry times, never IP addresses or devices | `PRESENCE_TTL_SECONDS` (60) after the last heartbeat |
| `push:throttle:<id>:<key>` | A flag saying a push was just sent | `PUSH_THROTTLE_SECONDS` (60) |
| `rl:message_request:…` | Daily request count | 24 hours |

### Push payloads

A push leaves the system through FCM (Google), and from there goes to Apple for
iOS devices. It carries:

- a title (the sender's display name, or the group chat's name);
- a body: "New message", or the text if the recipient opted in to previews;
- IDs: conversation, sequence number, type.

Activity pushes carry no content beyond names: "Kamau commented on your post". The
body is never logged (`app/core/providers/push.py`).

### Data rights in Phase 3

- **Export** adds the user's conversations (kind, title, role, status, joined),
  every message they sent (deleted ones included and marked), their devices (by the
  last eight characters of the token), their notification preferences, and their
  notifications. Messages **received** are not included: they are the other
  person's words (decision 3.17).
- **Purge** clears the text of every message the account sent, except under a
  pending report. It deletes their images through the media purge, takes them out
  of every chat (passing on any group they owned), and deletes their device tokens,
  notifications and preferences. Other people's conversations keep their shape,
  with "message deleted" in place of the purged account's messages.

---

## Phase 4 — Groups

Groups are where people hold power over each other's participation. Most of what
is stored here is that power — who is an admin, who is muted, who is banned and
why — and the rule throughout is that it is visible to the people who exercise it,
not to the people it is exercised on.

### `groups`

| Field | Why | Retention |
| --- | --- | --- |
| `name`, `description`, `rules`, `category` | What the group is | Life of the group; soft-deleted with it |
| `visibility`, `join_policy`, `requires_post_approval` | How it works | As above |
| `owner_id`, `created_by_id` | Who runs it, and who started it | Owner passes on purge; `created_by_id` cleared on purge |
| `cell_res9`…`cell_res6`, `centroid`, `area_id` | **Public groups only:** where it is discovered — the creator's home cell centre when it was made public | Cleared if the group becomes private. Never in a response: groups are placed by area label and distance band |
| `cover_media_id` | Cover image (purpose `group`) | Image deleted with the uploader's media on purge |
| `member_count`, `chat_conversation_id` | Counts and the linked chat | Life of the group |

### `group_members`

| Field | Why | Retention |
| --- | --- | --- |
| `user_id`, `role`, `status`, `joined_at`, `invited_by_id` | Membership, requests and invitations | Deleted on leaving, removal, rejection, or account purge |
| `muted_until` | A moderator's mute | Shown to moderators and above, and to the member on their own membership |
| `banned_at`, `banned_by_id`, `ban_reason` | A ban, kept so it holds | Until unbanned, or the banned account is purged. `ban_reason` is shown to the group's admins only |

### `group_invite_links`

| Field | Why | Retention |
| --- | --- | --- |
| `token_hash` | A keyed hash; the token itself is never stored | Deleted 30 days after expiry or revocation (`run_retention`) |
| `created_by_id`, `expires_at`, `max_uses`, `use_count`, `revoked_at` | Who made it and whether it still works | As above; links an account made are revoked on its purge |

### `posts` (changes)

`group_id` (now a foreign key), `approval_status` and `pinned_at`. A group post is
otherwise an ordinary post: the same location rule (the author's home cell), the
same export, the same purge. It never appears in the neighbourhood feed.

### Audit

Seven new actions, all admins acting on other people: `group_deleted`,
`group_member_removed`, `group_member_banned`, `group_member_unbanned`,
`group_role_changed`, `group_ownership_transferred`, `group_post_removed`.

### Data rights in Phase 4

- **Export** adds the groups a person belongs to (with role, status, joining date
  and any mute) and the groups they created. Their group posts were already in the
  content export. Bans placed on them are not listed (decision 4.13).
- **Purge** deletes every membership row, bans included. It passes on every group
  the account owned (admin, then moderator, then member), deleting any group left
  empty, and revokes the invite links the account created. Group posts are
  soft-deleted by the feed purge.

---

## Phase 5 — Marketplace

A listing is public by design, and the seller chose to publish it. What it must not
do is locate the seller. It is placed by an area label — the "approximate pickup
area" — and a distance band, exactly like a post. The meeting point is agreed in
chat.

### `listings`

| Field | Why | Retention |
| --- | --- | --- |
| `seller_id`, `title`, `description`, `category`, `condition`, `pricing`, `price_kes` | The listing | Soft-deleted by the seller or on purge; hard purge with other content in Phase 8 |
| `images` (`listing_images`) | Up to eight photos, EXIF-stripped like every upload | Deleted with the seller's media on purge |
| `cell_res9`…`cell_res6`, `centroid`, `area_id` | The seller's home cell when listed, for the radius filter | As the listing. Never in a response |
| `coarse_centroid` | The res-8 cell centre, only for sorting by distance band (decision 5.2) | As the listing. Never in a response |
| `availability`, `sold_at`, `sold_to_id` | Selling, and optionally who bought it — someone who asked about it in chat | As the listing; `sold_to_id` cleared if the buyer's account is purged |
| `expires_at`, `renewed_at` | 30-day listings, renewable | As the listing |
| `status`, `hidden_at` | Moderation, as for posts | As the listing |
| `content_hash`, `search_vector` | Duplicate detection and full-text search, derived from the text | As the listing |

### `saved_listings`

`user_id` and `listing_id`. Private to the user; nobody, the seller included, sees
who saved a listing. Deleted on purge.

### Listing conversations

Phase 3 conversations with `kind = listing` and `listing_id` set — the same tables,
the same rules and the same export. Nothing marketplace-specific is copied into
them.

### Data rights in Phase 5

- **Export** adds the user's listings (deleted and sold ones included and marked)
  and their saved listings.
- **Purge** soft-deletes their listings, whose photos go with the media purge, and
  deletes their saved listings. A listing they bought keeps its record of the sale,
  with `sold_to_id` cleared.

---

## Data subject rights — implementation status

| Right | Status | Where |
| --- | --- | --- |
| Access (export as JSON) | **Implemented** (through Phase 5) | `GET /v1/me/export`: account, consents, blocks, location summary, posts, comments, stories, reactions, uploads, group memberships and groups created, listings and saved listings, conversations, messages sent, devices, notification preferences and notifications |
| Rectification | **Implemented** | `PATCH /v1/me` |
| Erasure | **Implemented** (through Phase 5) | `POST /v1/me/deletion-request`, 30-day grace, then `run_retention` purges the account, its content, its group memberships (passing on groups it owned), its listings and saved listings, its messages, its devices and notifications, and its stored images |
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
| Stories | 24 hours, then hard-deleted with their images — held only while a report is pending |
| Home location | Only the current H3 cell and its centroid. No history: a change overwrites. |
| Explore-mode location | **Redis only, ~2 hour TTL. Never written to PostgreSQL.** Not in the export, because it is never at rest. |
| Home-presence history | A 14-bit rolling mask of daily yes/no answers to "was this user in their home area". Not the locations it came from. Self-purging. |
| Moderation reports and actions | 2 years, as the record of why an account was acted against |
| Audit log | 2 years |
| Payment records | 7 years, as tax law requires. **Survives account deletion**, and the deletion flow must say so. |
| Uploaded originals | Deleted immediately after processing |
| Idempotency records | 24 hours |
| Message text | Until deleted by its sender or the account is purged; kept only while a report on it is pending |
| Notifications | 90 days |
| Device tokens | Until signed out, reported dead by FCM, or 90 days without re-registering |
| Presence | Redis only, a minute after the last heartbeat |
| Group memberships and bans | Until left, removed, unbanned, or the account is purged |
| Group invite links | 30 days after they expire or are revoked |
| Listings | 30 days live, renewable; then readable by link until the seller deletes them or the account is purged |
| Application logs | 30 days |

---
