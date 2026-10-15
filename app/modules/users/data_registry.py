"""What happens to every reference to a person when their account is purged.

One entry per column that points at `users`. `tests/test_data_registry.py` fails
when a column pointing at `users` is missing from here, so a new table holding
personal data cannot ship without someone deciding, in writing, what its purge
does. `docs/DATA_MAP.md` gives the reasons at length; this is the checkable
summary.
"""

from __future__ import annotations

from enum import StrEnum


class Handling(StrEnum):
    erased = "erased"
    """The rows are deleted when the account is purged."""
    soft_deleted = "soft_deleted"
    """Soft-deleted at purge, then hard-deleted by the content retention job
    (`CONTENT_PURGE_AFTER_DAYS`), or held while a report about it is pending."""
    ended = "ended"
    """A membership: ended at purge (the person leaves), the row kept as history
    of the group or conversation."""
    pointer = "pointer"
    """A reference to the account, kept: it now points at the anonymised row —
    "Deleted user" — and holds nothing personal itself."""
    retained = "retained"
    """Kept deliberately past the purge, for the reason given, and deleted by its
    own retention rule."""


PURGE_REGISTRY: dict[str, tuple[Handling, str]] = {
    # -- Erased at purge ------------------------------------------------------
    "blocks.blocker_id": (Handling.erased, "Both directions"),
    "blocks.blocked_id": (Handling.erased, "Both directions"),
    "device_tokens.user_id": (Handling.erased, "Push addresses"),
    "notifications.user_id": (Handling.erased, "The in-app list"),
    "notification_preferences.user_id": (Handling.erased, "Settings"),
    "event_rsvps.user_id": (Handling.erased, "Who was going"),
    "event_reminders_sent.user_id": (Handling.erased, "Reminder ledger"),
    "saved_listings.user_id": (Handling.erased, "Saved items"),
    "reactions.user_id": (Handling.erased, "Reactions carry no content"),
    "story_views.viewer_id": (Handling.erased, "Who watched what"),
    "vouches.voucher_id": (Handling.erased, "Both directions"),
    "vouches.vouchee_id": (Handling.erased, "Both directions"),
    "user_warnings.user_id": (Handling.erased, "The audit log keeps that a warning was given"),
    # -- Soft-deleted, then hard-deleted by retention -------------------------
    "posts.author_id": (Handling.soft_deleted, "Evidence for pending reports"),
    "comments.author_id": (Handling.soft_deleted, "Evidence for pending reports"),
    "stories.author_id": (Handling.soft_deleted, "Evidence for pending reports"),
    "listings.seller_id": (Handling.soft_deleted, "Evidence for pending reports"),
    "service_posts.author_id": (Handling.soft_deleted, "Evidence for pending reports"),
    "events.organiser_id": (Handling.soft_deleted, "Evidence for pending reports"),
    "reviews.reviewer_id": (Handling.soft_deleted, "Evidence for pending reports"),
    "reviews.subject_id": (Handling.soft_deleted, "Reviews of the person go too"),
    "messages.sender_id": (
        Handling.soft_deleted,
        "Text cleared at purge; a tombstone stays so sequence numbers have no gaps",
    ),
    "media_assets.owner_id": (
        Handling.soft_deleted,
        "Objects deleted at purge; the stripped row goes with the content using it",
    ),
    # -- Memberships ended ----------------------------------------------------
    "conversation_members.user_id": (Handling.ended, "Leaves every conversation"),
    "group_members.user_id": (Handling.ended, "Leaves every group; ownership passes on"),
    # -- Pointers to the anonymised account -----------------------------------
    "conversation_members.invited_by_id": (Handling.pointer, "Who added someone to a chat"),
    "conversations.created_by_id": (Handling.pointer, "Who started a conversation"),
    "conversations.request_sender_id": (Handling.pointer, "Who sent a message request"),
    "event_rsvps.invited_by_id": (Handling.pointer, "Who invited someone"),
    "group_invite_links.created_by_id": (Handling.pointer, "Links are revoked at purge"),
    "group_members.banned_by_id": (Handling.pointer, "Which admin banned a member"),
    "group_members.invited_by_id": (Handling.pointer, "Who invited a member"),
    "groups.created_by_id": (Handling.pointer, "Cleared at purge"),
    "groups.owner_id": (Handling.pointer, "Ownership passes on at purge"),
    "listings.sold_to_id": (Handling.pointer, "The buyer a sale named"),
    "notifications.actor_id": (Handling.pointer, "Who caused someone else's notification"),
    # -- Retained ---------------------------------------------------------------
    "consents.user_id": (
        Handling.retained,
        "What was agreed to, and when — the Act's evidence; 2 years after the purge",
    ),
    "reports.reporter_id": (
        Handling.retained,
        "The record of why someone was acted against; 2 years after resolution",
    ),
    "device_sessions.user_id": (
        Handling.retained,
        "Revoked at purge, installation hash cleared; deleted 30 days after expiry",
    ),
    "banned_phones.user_id": (
        Handling.retained,
        "A ban outlives the account it was for; only a keyed hash is held",
    ),
    "banned_devices.user_id": (
        Handling.retained,
        "A ban outlives the account it was for; only a keyed hash is held",
    ),
}
