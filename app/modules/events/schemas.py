"""Request and response models for events.

The venue arrives as a point the organiser picked on a map (`VenueIn`) and leaves
as words: a name and directions, plus the area label. The point is never stored
and never returned. For a private event, the name and directions are null for
anyone who is not an accepted attendee (`venue_visible` says which applies).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.pagination import CursorPage
from app.modules.events.models import (
    AttendeeVisibility,
    EventStatus,
    EventType,
    EventVisibility,
    RsvpApproval,
    RsvpResponse,
)
from app.modules.feed.schemas import AuthorOut
from app.modules.location.cells import DistanceBucket
from app.modules.media.schemas import ImageVariantOut
from app.modules.moderation.models import ContentStatus
from app.modules.users.schemas import AreaLabelOut


class VenueIn(BaseModel):
    """Where the event is, picked on a map. Reduced to an H3 cell on arrival and
    discarded, like every coordinate the API receives."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=300, examples=["Kamakwa estate clean-up"])
    description: str | None = Field(default=None, max_length=20_000)
    event_type: EventType
    start_at: datetime = Field(
        description="With an offset, or without one to mean local time in `EVENT_TIMEZONE`.",
        examples=["2026-10-24T08:00:00"],
    )
    end_at: datetime = Field(examples=["2026-10-24T12:00:00"])
    venue: VenueIn
    venue_name: str = Field(min_length=1, max_length=500, examples=["Kamakwa primary school"])
    venue_details: str | None = Field(
        default=None, max_length=5000, description="Address and directions."
    )
    visibility: EventVisibility
    group_id: uuid.UUID | None = Field(
        default=None,
        description="Organise for a group you are an admin of. Required for `group` visibility.",
    )
    capacity: int | None = Field(default=None, ge=1)
    requires_approval: bool = False
    attendee_visibility: AttendeeVisibility = AttendeeVisibility.everyone
    cover_media_id: uuid.UUID | None = Field(
        default=None, description="A ready upload with purpose `event_cover`."
    )


class EventUpdateIn(BaseModel):
    """Only the fields sent are changed. A new time or venue tells everyone who
    replied."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    event_type: EventType | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    venue: VenueIn | None = None
    venue_name: str | None = Field(default=None, min_length=1, max_length=500)
    venue_details: str | None = Field(default=None, max_length=5000)
    capacity: int | None = Field(default=None, ge=1)
    requires_approval: bool | None = None
    attendee_visibility: AttendeeVisibility | None = None


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class RsvpIn(BaseModel):
    response: RsvpResponse


class ReviewRsvpIn(BaseModel):
    action: str = Field(pattern="^(approve|decline)$")


class InviteIn(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class InvitedOut(BaseModel):
    invited: list[uuid.UUID]


class LinkOut(BaseModel):
    token: str = Field(
        description="Shown once; only a hash is stored. Creating a new one ends the old."
    )


class MyRsvpOut(BaseModel):
    response: RsvpResponse | None = Field(description="Null when invited and not yet answered.")
    approval: RsvpApproval


class EventOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    event_type: EventType
    start_at: datetime = Field(description="UTC.")
    end_at: datetime = Field(description="UTC.")
    start_local: datetime = Field(description="The same instant in the event's zone, with offset.")
    end_local: datetime
    timezone: str = Field(examples=["Africa/Nairobi"])
    organiser: AuthorOut
    group_id: uuid.UUID | None = None
    area_label: AreaLabelOut
    distance_bucket: DistanceBucket | None = None
    venue_visible: bool = Field(
        description=(
            "False for a private event the caller has not been accepted to: the venue "
            "fields below are then null, and only the area label is given."
        )
    )
    venue_name: str | None = None
    venue_details: str | None = None
    cover: dict[str, ImageVariantOut] | None = None
    visibility: EventVisibility
    requires_approval: bool
    attendee_visibility: AttendeeVisibility
    capacity: int | None = None
    going_count: int
    interested_count: int
    is_full: bool
    event_status: EventStatus
    cancellation_reason: str | None = None
    my_rsvp: MyRsvpOut | None = None
    is_organiser: bool
    status: ContentStatus
    created_at: datetime


EventPageOut = CursorPage[EventOut]


class EventBrowseOut(BaseModel):
    items: list[EventOut]
    next_cursor: str | None = None
    radius_km: float = Field(description="How far this search reached, rounded to 0.5 km.")


class AttendeeOut(BaseModel):
    user: AuthorOut
    response: RsvpResponse | None
    approval: RsvpApproval


AttendeePageOut = CursorPage[AttendeeOut]
