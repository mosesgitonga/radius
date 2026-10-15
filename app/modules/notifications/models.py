"""Device tokens, notification preferences, and in-app notifications.

In-app notifications carry IDs, not content. "Kamau commented on your post" is
rendered by the client from the type, the actor, and the target; the comment text
is fetched from the comment. Copying content into notifications would make a second
store of everyone's words with its own retention problem — and a comment deleted by
its author would live on in someone else's notification list.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Distinct actors remembered per collapsed notification. Enough for "and 3 others";
# a viral post does not need the notification row to grow without bound.
MAX_REMEMBERED_ACTORS = 50


class NotificationType(StrEnum):
    """Plain strings in the database, so later phases add types without a migration."""

    message = "message"
    message_request = "message_request"
    chat_invite = "chat_invite"
    comment = "comment"
    reply = "reply"
    reaction = "reaction"
    group_invite = "group_invite"
    group_join_request = "group_join_request"
    """To a group's admins."""
    group_join_approved = "group_join_approved"
    group_post_approved = "group_post_approved"
    listing_inquiry = "listing_inquiry"
    """A buyer's first message about a seller's listing."""
    service_inquiry = "service_inquiry"
    """Someone's first message about a service post."""
    review_received = "review_received"
    review_reply = "review_reply"
    review_prompt = "review_prompt"
    """After a sale names the buyer: invite them to review the seller."""
    listing_expiring = "listing_expiring"
    event_invite = "event_invite"
    event_rsvp_request = "event_rsvp_request"
    """To the organiser, when an approval event gets an RSVP."""
    event_rsvp_approved = "event_rsvp_approved"
    event_reminder = "event_reminder"
    event_updated = "event_updated"
    event_cancelled = "event_cancelled"
    account_notice = "account_notice"
    """From the moderators: a warning, a suspension, a restriction, content taken
    down. The `data` says which; the push carries the wording."""


class DeviceToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An FCM registration token for one installed app.

    Stored in the clear because FCM needs the token itself to deliver. It is not a
    credential for this API — it only addresses a device — but it is personal data,
    and it is deleted when FCM reports it dead and when the account is purged.
    """

    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    device_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("device_sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    """The sign-in that registered this token. Pushes go only to tokens whose
    session is still live, so signing out of a phone stops its notifications at
    once — otherwise the next person to use a shared phone would see them."""
    token: Mapped[str] = mapped_column(String(4096), unique=True)
    platform: Mapped[str] = mapped_column(String(20))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationPreference(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per user per type, created only when it differs from the default."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32))
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    show_preview: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    """For message pushes: whether the lock screen shows the text, or only "New
    message from Kamau". Off by default, because phones in Kenya are often shared
    and a lock-screen preview is a message read by whoever picks the phone up."""

    __table_args__ = (
        UniqueConstraint("user_id", "type", name="uq_notification_preferences_user_id_type"),
    )


class Notification(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An in-app notification, possibly standing for a burst of events."""

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(32))

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    """The most recent actor; the one named first in "Kamau and 3 others"."""
    actor_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    event_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    collapse_key: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    """IDs only — see the module docstring."""

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The list, newest activity first; collapsed notifications bubble up.
        Index("ix_notifications_user_id_updated_at", "user_id", "updated_at"),
        # Finding the unread notification to collapse a new event into.
        Index(
            "ix_notifications_user_id_collapse_key_read_at", "user_id", "collapse_key", "read_at"
        ),
        Index("ix_notifications_created_at", "created_at"),
    )
