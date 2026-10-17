# Data map

What personal data this system holds, why it holds it, and how long it keeps it.
Maintained for the **Data Protection Act, 2019 (Kenya)**, which requires us to be
able to answer those three questions for every field, and to honour access,
rectification, and erasure requests across all of it.

**Rule for every phase:** a phase that introduces personal data updates this file
and extends the export and deletion routines in the same change. A field that is
not in this table cannot be exported or erased, because nobody knows it exists.

- **Data controller:** MyRadius. **Registration with the Office of the Data
  Protection Commissioner is still to be done, by the controller, before launch.**
  It is a legal filing, not a software task, and nothing in the code can complete it.
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
| `client_ip` | Abuse investigation, rate-limit forensics, incident response | 30 days, enforced by journald on the host (`docs/DEPLOYMENT.md` §2) | An IP address is personal data. It is the only identifier in Phase 0 logs. |
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
| `deleted_at` | "Message deleted" placeholder | The row stays so numbering holds. Content kept as evidence is erased 90 days after deletion (Phase 8) |

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

## Phase 6 — Jobs, services and reviews

### `service_posts`

| Field | Why | Retention |
| --- | --- | --- |
| `author_id`, `kind`, `category`, `title`, `description` | The post | Soft-deleted by the author or on purge |
| `rate_info`, `availability_info`, `travel_km` (offering); `budget_info` (seeking); `job_type`, `pay_min_kes`, `pay_max_kes`, `pay_period` (hiring) | What each kind needs | As the post |
| `cell_res9`…`cell_res6`, `centroid`, `area_id`, `coarse_centroid` | The author's home cell when posted, for the radius filter and band sorting | As the post. Never in a response |
| `expires_at`, `closed_at` | 30-day seeking and hiring posts; closing when filled | As the post |
| `status`, `hidden_at`, `flagged_phrases` | Moderation, and the fee-demand phrases that held a job post | As the post. `flagged_phrases` are shown to the author and to moderators |

`travel_km` is a number the provider chose — how far they will go — and reveals
nothing about where they are.

### `reviews`

| Field | Why | Retention |
| --- | --- | --- |
| `reviewer_id`, `subject_id`, `context_type`, `context_id`, `conversation_id` | Who reviewed whom about what, and the accepted conversation that allowed it | Soft-deleted by the reviewer, or when either account is purged |
| `rating`, `body`, `reply_body`, `replied_at` | The review and the reply | As above |
| `status`, `hidden_at` | Moderation | As above |

`users.rating_average` and `users.rating_count` (Phase 1 columns) are now filled:
derived from visible reviews, recomputed on every change.

### Conversations

`conversations.service_post_id` and the `service` kind — conversations about a
service post, with no new personal data of their own.

### Data rights in Phase 6

- **Export** adds the user's service posts, and the reviews they wrote and
  received. Reviews are public statements about a person, so the ones about them
  are theirs to see, unlike private messages received (decision 6.8).
- **Purge** soft-deletes their service posts and every review they wrote or
  received, then recomputes the ratings of everyone they had reviewed.

---

## Phase 7 — Events

An event is a public or semi-public notice, but its venue can be someone's home —
a private party, a chama meeting. So the venue is held as words, shown to the
right people, and the place itself only as a coarse cell.

### `events`

| Field | Why | Retention |
| --- | --- | --- |
| `organiser_id`, `group_id`, `title`, `description`, `event_type` | The event | Soft-deleted by the organiser; cancelled and deleted on the organiser's purge |
| `start_at`, `end_at` | When, in UTC | As the event |
| `venue_name`, `venue_details` | The exact venue, as words. **Private events: shown only to the organiser and accepted attendees** | As the event |
| `cell_res9`…`cell_res6`, `centroid`, `coarse_centroid` | The venue's cell, from a map point that is discarded on receipt. No coordinate column exists | As the event. Never in a response |
| `visibility`, `requires_approval`, `attendee_visibility`, `capacity`, counts | How it works | As the event |
| `invite_token_hash` | A private event's link, hashed | Replaced or cleared by the organiser |
| `event_status`, `cancelled_at`, `cancellation_reason` | Cancellation | As the event |
| `status`, `hidden_at` | Moderation | As the event |

### `event_rsvps`

`user_id`, `response` (going, interested, not going, or none for an unanswered
invitation), `approval`, `invited_by_id`. Who can see that a person is attending
follows the event's attendee visibility — everyone, attendees only, or the
organiser only — and blocked people are always left out. Deleted on purge.

### `event_reminders_sent`

The record that a reminder was sent: event, person, offset, time. It exists to
make reminders idempotent. Cleared when an event's time changes, and deleted with
the person.

### `listings.expiry_reminded_at`

When the seller was told the listing is about to expire. Cleared on renewal.

### Data rights in Phase 7

- **Export** adds the events the user organised (with their venues — the user's
  own words) and their replies.
- **Purge** cancels any upcoming event the account organised, which tells everyone
  who replied, and soft-deletes them all. It deletes the account's replies and
  reminder records, then recounts the events they had replied to.

---

## Phase 8 — Trust, safety and launch hardening

Phase 8 adds the people who moderate, the records of what they do, and the ban
lists. It also adds, for the first time, a check that every reference to a person
has a written purge decision (`app/modules/users/data_registry.py`, enforced by
`tests/test_data_registry.py`).

### `staff_accounts` and `staff_sessions`

Staff are not users. Their data is about employment, not the community.

| Field | Why | Retention |
| --- | --- | --- |
| `email`, `display_name`, `role` | Who the moderator is, and what they may do | While employed; a disabled account is kept, because the audit log names it |
| `password_hash` | scrypt, with its parameters | Until changed |
| `totp_secret`, `totp_last_step` | The authenticator factor, and replay protection | Until reset. Unencrypted at rest; see `docs/SECURITY.md`, open item 2 |
| `failed_logins`, `locked_until`, `last_login_at`, `disabled_at` | Lockout and account state | As the account |
| Session: `staff_id`, `expires_at`, `revoked_at`, `ip_address` | One sign-in, for revocation and forensics | 8 hours live; kept as history |

### `user_warnings`

| Field | Why | Retention |
| --- | --- | --- |
| `user_id`, `reason`, `staff_id`, `report_id`, `created_at` | A moderator's warning. The person sees it in `GET /v1/me/standing`, and it counts in their history | 2 years, or until the account is purged. The audit log keeps the fact of a warning; the wording goes |

### `banned_phones` and `banned_devices`

| Field | Why | Retention |
| --- | --- | --- |
| `phone_hash` / `device_hash` | **A keyed HMAC, never the number or the installation ID.** Checked at sign-in | Until a moderator lifts the ban. Survives the account's purge: a ban outlives the account it was for |
| `user_id`, `staff_id`, `reason` | Which account the ban came from, who imposed it and why | As the entry. `user_id` points at the anonymised row after a purge |

The hash is keyed with `BAN_LIST_KEY`, so a leaked table cannot be checked against
a list of numbers without the key.

### `vouches`

`voucher_id`, `vouchee_id`, `created_at`: a verified neighbour vouching for
someone. Counted towards the badge only while the voucher is verified and lives in
the same res-7 area. That is checked when the badge is computed, never stored.
Withdrawable by the voucher. Deleted in both directions on purge, and exported.

### `users` (changes)

| Field | Why | Retention |
| --- | --- | --- |
| `suspended_until` | When a suspension lifts on its own | Cleared when lifted, and on purge |
| `restricted_until`, `restriction_reason` | A restriction, by a moderator or by reports. The reason is shown to the person | Lapses on its own; cleared on purge |
| `badge_locked_at` | A moderator's lock on the badge | Until unlocked; cleared on purge |

### `device_sessions.device_id_hash`

A keyed hash of the installation ID the app sends at sign-in. It lets a ban reach
the devices an account used. The ID itself is never stored. Cleared from every
session of an account when the account is purged.

### Audit

Every staff action is an audit event with actor type `moderator`. Staff sign-ins
and failed sign-ins are audited too, but never with the email that was tried.
Badge awards and revocations by the nightly job have actor type `system`.

### Outside the database

- **Sentry**, when configured, receives errors with no headers, query strings,
  bodies, local variables, IP addresses or user details beyond an opaque ID.
  Strings are redacted with the log rules. It acts as a processor.
- **Metrics** carry route templates, status codes and task names. No identifiers.
- **Backups:** `pg_dump` files, kept 14 days, locally (mode 700) and in a private
  bucket. They contain everything the database does. **A purged account remains in
  backups until they age out, two weeks after the purge**, and the deletion flow
  should say so.

### Retention, now enforced

The nightly `run_retention` job now also:

- **Content:** hard-deletes content soft-deleted or removed more than
  `CONTENT_PURGE_AFTER_DAYS` (90) ago, with its images. Posts, comments, stories,
  listings, service posts, events, reviews and groups are covered. Anything under a
  pending report is kept, and so is anything whose deletion would cascade to
  something under a pending report.
- **Messages:** erases the content kept as evidence in deleted messages after the
  same period. The "message deleted" tombstone stays.
- **Records:** deletes audit events, resolved reports and warnings older than
  `AUDIT_RETENTION_DAYS` (730).
- **Consents:** deletes the consent records of accounts purged more than
  `AUDIT_RETENTION_DAYS` ago.

### Data rights in Phase 8

- **Export** adds:
  - the account's standing: suspension end, restriction and its reason;
  - the vouches given and the number received;
  - the warnings received;
  - every report the person filed, with its outcome.

  It never says who reported *them*.
- **Purge** adds:
  - deleting blocks in both directions. Documented since Phase 1 and not done until
    now; found by the new registry test.
  - vouches in both directions, and warnings;
  - the installation hashes on the account's sessions;
  - clearing the restriction, suspension and badge-lock fields.
- **Restriction of processing:** a moderator or the report rule can now restrict an
  account (see the table below).

---

## The mobile app — what stays on the phone

| What | Where | Why | Kept until |
| --- | --- | --- | --- |
| Refresh token | Keychain / Android Keystore-backed secure storage | Staying signed in | Sign-out, or the server refusing it |
| Access token | Memory only | Calling the API | The app closes, or it expires (15 min) |
| Installation ID | Secure storage | Sent at sign-in so a person can recognise their devices, and so a ban can reach a device (stored server-side only as a keyed hash) | The app is uninstalled |
| Feed photos | The image cache in the app's cache folder | Not downloading the same image twice | Evicted by size and age, or cleared by the system. **Not yet cleared at sign-out** — to do before shared-phone use is likely |
| Photos chosen for a post | Resized copies in the app's cache folder, made by the picker | Uploading | Cleared by the system. Location data is stripped server-side whatever the phone sends |

The app keeps no copy of the feed, messages or the account's details on disk.

---

## Data subject rights — implementation status

| Right | Status | Where |
| --- | --- | --- |
| Access (export as JSON) | **Implemented** (through Phase 8) | `GET /v1/me/export`: account, consents, blocks, location summary, posts, comments, stories, reactions, uploads, group memberships and groups created, listings and saved listings, service posts, reviews written and received, events organised and replied to, conversations, messages sent, devices, notification preferences and notifications, standing, vouches, warnings received and reports filed |
| Rectification | **Implemented** | `PATCH /v1/me` |
| Erasure | **Implemented** (through Phase 8) | `POST /v1/me/deletion-request`, 30-day grace, then `run_retention` purges the account, its content, its group memberships (passing on groups it owned), its listings and saved listings, its service posts and reviews, its events and replies, its messages, its devices and notifications, its stored images, its blocks, vouches and warnings, and the installation hashes on its sessions. Content and records are then hard-deleted by retention |
| Restriction of processing | **Implemented** | Moderators suspend or restrict an account (`/v1/admin/users/{id}/suspend`, `/restrict`); the person sees it at `GET /v1/me/standing` |
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
| Soft-deleted user content | 90 days, then hard-deleted with its images (Phase 8 `run_retention`); held while a report about it, or about anything it would cascade to, is pending |
| Stories | 24 hours, then hard-deleted with their images — held only while a report is pending |
| Home location | Only the current H3 cell and its centroid. No history: a change overwrites. |
| Explore-mode location | **Redis only, ~2 hour TTL. Never written to PostgreSQL.** Not in the export, because it is never at rest. |
| Home-presence history | A 14-bit rolling mask of daily yes/no answers to "was this user in their home area". Not the locations it came from. Self-purging. |
| Moderation reports and warnings | 2 years after resolution (Phase 8 `run_retention`); pending reports are never deleted |
| Audit log | 2 years (Phase 8 `run_retention`) |
| Consent records | 2 years after the account is purged (Phase 8 `run_retention`) |
| Ban-list entries | Until lifted; keyed hashes only, surviving the account's purge |
| Staff accounts | While employed; disabled accounts kept for the audit log's references |
| Database backups | 14 days, locally and off-site |
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
| Seeking and hiring posts | 30 days, reopenable; offerings until deleted |
| Reviews | Until deleted by the reviewer, or either account is purged |
| Events | Until deleted by the organiser, or the organiser's account is purged |
| Event reminder records | Until the event's time changes, or the person is purged |
| Application logs | 30 days |

---
