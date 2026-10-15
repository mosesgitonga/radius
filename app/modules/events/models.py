"""Events, RSVPs, and the record of reminders sent.

**Where an event is.** The organiser picks the venue on a map. The coordinates are
turned into a res-9 cell on receipt and discarded, exactly as for a user's home.
The cell and its centre drive discovery and the area label. The *exact* venue — a
name and directions — is text the organiser writes, and for a private event it is
shown only to accepted attendees (the brief's location-privacy rule). No
coordinate is stored, and none is ever returned.

**Times are instants.** `start_at` and `end_at` are UTC. The local zone
(`EVENT_TIMEZONE`) matters only at the edges: reading what a person typed, showing
it back, wording reminders, and deciding what "this weekend" means.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, db_enum
from app.modules.feed.models import LocatedContentMixin
from app.modules.moderation.models import ContentStatus


class EventType(StrEnum):
    party = "party"
    church = "church"
    sports = "sports"
    harambee = "harambee"
    clean_up = "clean_up"
    meetup = "meetup"
    other = "other"


class EventVisibility(StrEnum):
    public = "public"
    """Discoverable within the events radius."""
    group = "group"
    """For one group's active members."""
    private = "private"
    """Invite-only, by invitation or link. The venue is for accepted attendees."""


class AttendeeVisibility(StrEnum):
    everyone = "everyone"
    """Anyone who can see the event sees who is going."""
    attendees = "attendees"
    """Only people going or interested see the list."""
    organiser = "organiser"
    """Only the organiser. Everyone else sees the counts."""


class EventStatus(StrEnum):
    scheduled = "scheduled"
    cancelled = "cancelled"


class RsvpResponse(StrEnum):
    going = "going"
    interested = "interested"
    not_going = "not_going"


class RsvpApproval(StrEnum):
    approved = "approved"
    pending = "pending"
    """Waiting for the organiser, in an event that approves attendees."""
    declined = "declined"


_type = db_enum(EventType, name="event_type", length=16)
_visibility = db_enum(EventVisibility, name="event_visibility", length=16)
_attendee_visibility = db_enum(AttendeeVisibility, name="attendee_visibility", length=16)
_status = db_enum(EventStatus, name="event_status", length=16)
_response = db_enum(RsvpResponse, name="rsvp_response", length=16)
_approval = db_enum(RsvpApproval, name="rsvp_approval", length=16)
_content_status = db_enum(ContentStatus, name="content_status", length=32)


class Event(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, LocatedContentMixin):
    __tablename__ = "events"

    organiser_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    """Set when a group organises it; the organiser is the admin who created it."""

    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(String(20_000), default="", server_default="")
    event_type: Mapped[EventType] = mapped_column(_type)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    venue_name: Mapped[str] = mapped_column(String(500))
    venue_details: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    """Address and directions — the exact venue. Private events show it only to
    accepted attendees."""
    coarse_centroid: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )

    cover_media_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Limits "going". Interest is unlimited."""

    visibility: Mapped[EventVisibility] = mapped_column(_visibility)
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    attendee_visibility: Mapped[AttendeeVisibility] = mapped_column(
        _attendee_visibility,
        default=AttendeeVisibility.everyone,
        server_default=AttendeeVisibility.everyone.value,
    )
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    """A shareable link into a private event, stored hashed like group links."""

    event_status: Mapped[EventStatus] = mapped_column(
        _status, default=EventStatus.scheduled, server_default=EventStatus.scheduled.value
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    going_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    interested_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    status: Mapped[ContentStatus] = mapped_column(
        _content_status,
        default=ContentStatus.visible,
        server_default=ContentStatus.visible.value,
    )
    """Moderation, named as on posts so the auto-hide rule applies unchanged."""
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("end_at > start_at", name="event_ends_after_it_starts"),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="event_capacity_positive"),
        CheckConstraint(
            "visibility <> 'group' OR group_id IS NOT NULL", name="event_group_visibility_has_group"
        ),
        CheckConstraint(
            "invite_token_hash IS NULL OR visibility = 'private'",
            name="event_link_only_for_private",
        ),
        CheckConstraint(
            "going_count >= 0 AND interested_count >= 0", name="event_counts_non_negative"
        ),
        CheckConstraint(
            "event_status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="event_cancelled_has_time",
        ),
        CheckConstraint("length(title) > 0", name="event_title_not_empty"),
        Index("ix_events_visibility_start_at_id", "visibility", "start_at", "id"),
    )


class EventRsvp(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "event_rsvps"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    response: Mapped[RsvpResponse | None] = mapped_column(_response, nullable=True)
    """Null for someone invited who has not answered."""
    approval: Mapped[RsvpApproval] = mapped_column(
        _approval, default=RsvpApproval.approved, server_default=RsvpApproval.approved.value
    )
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_event_rsvps_event_id_user_id"),
        Index("ix_event_rsvps_event_id_response", "event_id", "response"),
    )

    @property
    def is_accepted(self) -> bool:
        """An attendee who may see a private event's venue."""
        return self.approval == RsvpApproval.approved and self.response in (
            RsvpResponse.going,
            RsvpResponse.interested,
        )


class EventReminderSent(Base, UUIDPrimaryKeyMixin):
    """One reminder, to one person, at one offset. The unique constraint is what
    makes the sweep idempotent: a worker retried or run twice cannot remind twice."""

    __tablename__ = "event_reminders_sent"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    offset_minutes: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "user_id",
            "offset_minutes",
            name="uq_event_reminders_sent_event_id_user_id_offset_minutes",
        ),
    )
