"""Events: organising, finding, replying, reminding.

Counts are recomputed, not incremented: after any RSVP change, `going_count` and
`interested_count` are set from the approved RSVPs in one statement, so capacity
checks and the numbers shown can never drift from the rows.

Reminders are a sweep, not scheduled jobs. Every minute the worker asks which
reminders are due now (`times.reminder_due`), records each one it sends in
`event_reminders_sent`, and sends only those it managed to record. A sweep run
twice, a worker restarted mid-run, or an event moved to a new time all resolve to
"each attendee gets each reminder once, at the right moment".
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.pagination import PageParams, paginate
from app.core.security import generate_opaque_token, hash_secret
from app.modules.events import access, times
from app.modules.events.access import EventView
from app.modules.events.models import (
    AttendeeVisibility,
    Event,
    EventReminderSent,
    EventRsvp,
    EventStatus,
    EventType,
    EventVisibility,
    RsvpApproval,
    RsvpResponse,
)
from app.modules.groups import access as group_access
from app.modules.groups.permissions import GroupAction, require
from app.modules.location import bands, cells
from app.modules.location.radius import RadiusEngine
from app.modules.location.service import AnchorMode, resolve_anchor
from app.modules.marketplace.models import Listing, ListingAvailability
from app.modules.media.models import MediaPurpose
from app.modules.media.service import claim_assets_for
from app.modules.moderation.models import ContentStatus
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users.models import User, UserStatus
from app.modules.users.service import blocked_user_ids, get_user, is_blocked_between

logger = logging.getLogger(__name__)

MATERIAL_FIELDS = frozenset({"start_at", "end_at", "venue_name", "venue_details", "venue"})
"""Changes worth telling attendees about. A reworded description is not."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _clean(value: Any, *, field: str, max_length: int, required: bool = False) -> str | None:
    cleaned = (str(value) if value is not None else "").strip()
    if required and not cleaned:
        raise BadRequestError(f"An event needs a {field}")
    if len(cleaned) > max_length:
        raise BadRequestError(
            f"That {field} is too long", details={"field": field, "max_length": max_length}
        )
    return cleaned or None


def _check_times(
    start_at: datetime, end_at: datetime, *, now: datetime, settings: Settings
) -> None:
    if end_at <= start_at:
        raise BadRequestError("An event must end after it starts")
    if end_at <= now:
        raise BadRequestError("That event would already be over")
    if end_at - start_at > timedelta(days=settings.event_max_duration_days):
        raise BadRequestError(
            "That event is too long", details={"max_days": settings.event_max_duration_days}
        )


def _place(event: Event, latitude: float, longitude: float) -> None:
    """The venue's cell, from coordinates that are discarded right here."""
    located = cells.locate(latitude, longitude)
    event.cell_res9 = located.cell_res9
    event.cell_res8 = located.cell_res8
    event.cell_res7 = located.cell_res7
    event.cell_res6 = located.cell_res6
    event.centroid = located.centroid_wkt
    event.coarse_centroid = bands.coarse_wkt(located.cell_res8)


async def _require_group_organiser(
    session: AsyncSession, *, user: User, group_id: uuid.UUID
) -> None:
    view = await group_access.member_group(session, group_id=group_id, viewer_id=user.id)
    assert view.role is not None  # noqa: S101 — an active member has a role
    require(view.role, GroupAction.manage_events)


async def _recount(session: AsyncSession, event_id: uuid.UUID) -> None:
    def counted(response: RsvpResponse) -> Any:
        return (
            select(func.count(EventRsvp.id))
            .where(
                EventRsvp.event_id == event_id,
                EventRsvp.response == response,
                EventRsvp.approval == RsvpApproval.approved,
            )
            .scalar_subquery()
        )

    await session.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(
            going_count=counted(RsvpResponse.going),
            interested_count=counted(RsvpResponse.interested),
        )
    )


# ---------------------------------------------------------------------------
# Organising
# ---------------------------------------------------------------------------
async def create_event(
    session: AsyncSession,
    *,
    organiser: User,
    fields: dict[str, Any],
    settings: Settings,
) -> Event:
    now = datetime.now(UTC)
    zone = settings.event_zone
    start_at = times.to_utc(fields["start_at"], zone)
    end_at = times.to_utc(fields["end_at"], zone)
    _check_times(start_at, end_at, now=now, settings=settings)

    visibility = EventVisibility(fields["visibility"])
    group_id = fields.get("group_id")
    if group_id is not None:
        await _require_group_organiser(session, user=organiser, group_id=group_id)
    elif visibility == EventVisibility.group:
        raise BadRequestError("A group-only event needs a group")

    capacity = fields.get("capacity")
    if capacity is not None and capacity > settings.event_max_capacity:
        raise BadRequestError("That capacity is too large")

    event = Event(
        organiser_id=organiser.id,
        group_id=group_id,
        title=_clean(
            fields["title"],
            field="title",
            max_length=settings.event_title_max_length,
            required=True,
        )
        or "",
        description=_clean(
            fields.get("description"),
            field="description",
            max_length=settings.event_description_max_length,
        )
        or "",
        event_type=EventType(fields["event_type"]),
        start_at=start_at,
        end_at=end_at,
        venue_name=_clean(
            fields["venue_name"],
            field="venue name",
            max_length=settings.event_venue_name_max_length,
            required=True,
        )
        or "",
        venue_details=_clean(
            fields.get("venue_details"),
            field="venue details",
            max_length=settings.event_venue_details_max_length,
        ),
        capacity=capacity,
        visibility=visibility,
        requires_approval=bool(fields.get("requires_approval", False)),
        attendee_visibility=AttendeeVisibility(
            fields.get("attendee_visibility", AttendeeVisibility.everyone)
        ),
    )
    _place(event, fields["venue"]["latitude"], fields["venue"]["longitude"])
    if fields.get("cover_media_id") is not None:
        (asset,) = await claim_assets_for(
            session,
            media_ids=[fields["cover_media_id"]],
            owner_id=organiser.id,
            purpose=MediaPurpose.event_cover,
        )
        event.cover_media_id = asset.id
    session.add(event)
    await session.flush()
    # The organiser is going to their own event, and is reminded like anyone else.
    session.add(EventRsvp(event_id=event.id, user_id=organiser.id, response=RsvpResponse.going))
    await session.flush()
    await _recount(session, event.id)
    await session.refresh(event)
    logger.info(
        "event_created", extra={"visibility": str(visibility), "type": str(event.event_type)}
    )
    return event


async def _manageable(session: AsyncSession, *, user: User, event_id: uuid.UUID) -> Event:
    """An event the caller may change: theirs, or their group's if they are one of
    its admins."""
    view = await access.visible_event(session, event_id=event_id, viewer_id=user.id, lock=True)
    event = view.event
    if event.organiser_id == user.id:
        return event
    if event.group_id is not None:
        try:
            await _require_group_organiser(session, user=user, group_id=event.group_id)
        except (ForbiddenError, NotFoundError):
            pass
        else:
            return event
    raise ForbiddenError("Only the organiser can change this event")


async def _rsvp_recipients(session: AsyncSession, event: Event) -> list[uuid.UUID]:
    """Everyone who replied — going, interested, not going, or waiting — less the
    organiser."""
    return [
        user_id
        for user_id in (
            await session.execute(
                select(EventRsvp.user_id).where(
                    EventRsvp.event_id == event.id, EventRsvp.response.is_not(None)
                )
            )
        ).scalars()
        if user_id != event.organiser_id
    ]


async def _tell_attendees(
    session: AsyncSession,
    event: Event,
    *,
    kind: NotificationType,
    actor_id: uuid.UUID,
    settings: Settings,
) -> int:
    recipients = await _rsvp_recipients(session, event)
    for user_id in recipients:
        await notifications_service.notify(
            session,
            recipient_id=user_id,
            kind=kind,
            actor_id=actor_id,
            target_type="event",
            target_id=event.id,
            collapse_key=f"{kind.value}:{event.id}",
            data={"event_id": str(event.id)},
            settings=settings,
        )
    return len(recipients)


async def update_event(
    session: AsyncSession,
    *,
    user: User,
    event_id: uuid.UUID,
    changes: dict[str, Any],
    settings: Settings,
) -> Event:
    """Change an event. A new time or venue tells everyone who replied, and resets
    the reminders, which were due relative to the old time."""
    event = await _manageable(session, user=user, event_id=event_id)
    if event.event_status == EventStatus.cancelled:
        raise ConflictError("A cancelled event cannot be changed")
    zone = settings.event_zone
    material = bool(MATERIAL_FIELDS & {key for key, value in changes.items() if value is not None})

    start_at = (
        times.to_utc(changes["start_at"], zone) if changes.get("start_at") else event.start_at
    )
    end_at = times.to_utc(changes["end_at"], zone) if changes.get("end_at") else event.end_at
    if "start_at" in changes or "end_at" in changes:
        _check_times(start_at, end_at, now=datetime.now(UTC), settings=settings)
        event.start_at, event.end_at = start_at, end_at
    if changes.get("title") is not None:
        event.title = (
            _clean(
                changes["title"],
                field="title",
                max_length=settings.event_title_max_length,
                required=True,
            )
            or event.title
        )
    if "description" in changes:
        event.description = (
            _clean(
                changes["description"],
                field="description",
                max_length=settings.event_description_max_length,
            )
            or ""
        )
    if changes.get("venue_name") is not None:
        event.venue_name = (
            _clean(
                changes["venue_name"],
                field="venue name",
                max_length=settings.event_venue_name_max_length,
                required=True,
            )
            or event.venue_name
        )
    if "venue_details" in changes:
        event.venue_details = _clean(
            changes["venue_details"],
            field="venue details",
            max_length=settings.event_venue_details_max_length,
        )
    if changes.get("venue") is not None:
        _place(event, changes["venue"]["latitude"], changes["venue"]["longitude"])
    if changes.get("event_type") is not None:
        event.event_type = EventType(changes["event_type"])
    if "capacity" in changes:
        capacity = changes["capacity"]
        if capacity is not None and capacity < event.going_count:
            raise ConflictError(
                "More people are already going than that",
                details={"going": event.going_count},
            )
        event.capacity = capacity
    if changes.get("attendee_visibility") is not None:
        event.attendee_visibility = AttendeeVisibility(changes["attendee_visibility"])
    if changes.get("requires_approval") is not None:
        event.requires_approval = bool(changes["requires_approval"])
    await session.flush()

    if material:
        if "start_at" in changes or "end_at" in changes:
            await execute_dml(
                session, delete(EventReminderSent).where(EventReminderSent.event_id == event.id)
            )
        await _tell_attendees(
            session, event, kind=NotificationType.event_updated, actor_id=user.id, settings=settings
        )
    await session.refresh(event)
    return event


async def cancel_event(
    session: AsyncSession,
    *,
    user: User,
    event_id: uuid.UUID,
    reason: str | None,
    settings: Settings,
) -> Event:
    event = await _manageable(session, user=user, event_id=event_id)
    if event.event_status == EventStatus.cancelled:
        return event
    event.event_status = EventStatus.cancelled
    event.cancelled_at = datetime.now(UTC)
    event.cancellation_reason = _clean(reason, field="reason", max_length=1000)
    await session.flush()
    await _tell_attendees(
        session, event, kind=NotificationType.event_cancelled, actor_id=user.id, settings=settings
    )
    await session.refresh(event)
    return event


async def delete_event(
    session: AsyncSession, *, user: User, event_id: uuid.UUID, settings: Settings
) -> None:
    """Deleting an upcoming event cancels it first, so nobody turns up to nothing."""
    event = await _manageable(session, user=user, event_id=event_id)
    if event.event_status == EventStatus.scheduled and event.end_at > datetime.now(UTC):
        await cancel_event(session, user=user, event_id=event_id, reason=None, settings=settings)
    event.deleted_at = datetime.now(UTC)
    await session.flush()


# ---------------------------------------------------------------------------
# Invitations and the link
# ---------------------------------------------------------------------------
async def invite(
    session: AsyncSession,
    *,
    user: User,
    event_id: uuid.UUID,
    user_ids: Sequence[uuid.UUID],
    settings: Settings,
) -> list[uuid.UUID]:
    """Invite people. Invited people are approved already: the organiser chose them."""
    event = await _manageable(session, user=user, event_id=event_id)
    invited: list[uuid.UUID] = []
    for user_id in dict.fromkeys(user_ids):
        if user_id == event.organiser_id:
            continue
        other = await get_user(session, user_id)
        if (
            other is None
            or other.deleted_at is not None
            or other.status == UserStatus.banned
            or await is_blocked_between(session, user.id, user_id)
        ):
            raise NotFoundError("User not found")
        if await access.rsvp_of(session, event.id, user_id) is not None:
            continue
        session.add(EventRsvp(event_id=event.id, user_id=user_id, invited_by_id=user.id))
        invited.append(user_id)
    await session.flush()
    for user_id in invited:
        await notifications_service.notify(
            session,
            recipient_id=user_id,
            kind=NotificationType.event_invite,
            actor_id=user.id,
            target_type="event",
            target_id=event.id,
            collapse_key=f"event_invite:{event.id}",
            data={"event_id": str(event.id)},
            settings=settings,
        )
    return invited


async def create_link(
    session: AsyncSession, *, user: User, event_id: uuid.UUID, settings: Settings
) -> str:
    """A new link into a private event. Replaces any earlier one, which stops
    working — the way to "unshare" a link that travelled too far."""
    event = await _manageable(session, user=user, event_id=event_id)
    if event.visibility != EventVisibility.private:
        raise BadRequestError("Only a private event needs a link; share the event itself")
    token = generate_opaque_token()
    event.invite_token_hash = hash_secret(token, settings)
    await session.flush()
    return token


async def revoke_link(session: AsyncSession, *, user: User, event_id: uuid.UUID) -> None:
    event = await _manageable(session, user=user, event_id=event_id)
    event.invite_token_hash = None
    await session.flush()


async def event_for_link(
    session: AsyncSession, *, viewer: User, token: str, settings: Settings
) -> Event:
    event = (
        await session.execute(
            select(Event).where(
                Event.invite_token_hash == hash_secret(token, settings),
                Event.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if (
        event is None
        or event.status != ContentStatus.visible
        or await is_blocked_between(session, viewer.id, event.organiser_id)
    ):
        raise NotFoundError("That event link is not valid")
    return event


# ---------------------------------------------------------------------------
# Replying
# ---------------------------------------------------------------------------
async def rsvp(
    session: AsyncSession,
    *,
    user: User,
    event_id: uuid.UUID,
    response: RsvpResponse,
    settings: Settings,
    via_link: Event | None = None,
) -> EventView:
    """Reply to an event.

    In an approval event, a new reply from someone not invited waits for the
    organiser. Capacity limits "going". Cancelled and finished events take no
    replies.
    """
    if via_link is not None:
        event = await access.load_event(session, via_link.id, lock=True)
        existing = await access.rsvp_of(session, event.id, user.id)
    else:
        view = await access.visible_event(session, event_id=event_id, viewer_id=user.id, lock=True)
        event, existing = view.event, view.rsvp
    now = datetime.now(UTC)
    if event.event_status == EventStatus.cancelled:
        raise ConflictError("This event has been cancelled")
    if event.end_at <= now:
        raise ConflictError("This event is over")
    if event.organiser_id == user.id and response != RsvpResponse.going:
        raise BadRequestError("The organiser is going to their own event")

    if existing is None:
        existing = EventRsvp(
            event_id=event.id,
            user_id=user.id,
            approval=RsvpApproval.pending if event.requires_approval else RsvpApproval.approved,
        )
        session.add(existing)
    elif existing.approval == RsvpApproval.declined:
        raise ConflictError("The organiser did not confirm your place")

    if (
        response == RsvpResponse.going
        and existing.approval == RsvpApproval.approved
        and existing.response != RsvpResponse.going
        and event.capacity is not None
        and event.going_count >= event.capacity
    ):
        raise ConflictError("This event is full", details={"capacity": event.capacity})

    newly_pending = existing.approval == RsvpApproval.pending and existing.response is None
    existing.response = response
    await session.flush()
    await _recount(session, event.id)
    if newly_pending:
        await notifications_service.notify(
            session,
            recipient_id=event.organiser_id,
            kind=NotificationType.event_rsvp_request,
            actor_id=user.id,
            target_type="event",
            target_id=event.id,
            collapse_key=f"event_rsvp_request:{event.id}",
            data={"event_id": str(event.id)},
            settings=settings,
        )
    await session.refresh(event)
    return EventView(event=event, rsvp=existing)


async def review_rsvp(
    session: AsyncSession,
    *,
    user: User,
    event_id: uuid.UUID,
    attendee_id: uuid.UUID,
    approve: bool,
    settings: Settings,
) -> None:
    event = await _manageable(session, user=user, event_id=event_id)
    pending = await access.rsvp_of(session, event.id, attendee_id)
    if pending is None or pending.approval != RsvpApproval.pending:
        raise NotFoundError("There is no request from that person")
    if (
        approve
        and pending.response == RsvpResponse.going
        and event.capacity is not None
        and event.going_count >= event.capacity
    ):
        raise ConflictError("This event is full", details={"capacity": event.capacity})
    pending.approval = RsvpApproval.approved if approve else RsvpApproval.declined
    await session.flush()
    await _recount(session, event.id)
    if approve:
        await notifications_service.notify(
            session,
            recipient_id=attendee_id,
            kind=NotificationType.event_rsvp_approved,
            actor_id=user.id,
            target_type="event",
            target_id=event.id,
            collapse_key=f"event_rsvp_approved:{event.id}",
            data={"event_id": str(event.id)},
            settings=settings,
        )


async def attendees(
    session: AsyncSession,
    *,
    viewer: User,
    event_id: uuid.UUID,
    response: RsvpResponse | None,
    pending: bool,
    params: PageParams,
) -> tuple[list[EventRsvp], str | None]:
    """Who replied, as far as the event lets the viewer see. Requests waiting for
    approval are for the organiser only. Anyone blocked either way is left out."""
    view = await access.visible_event(session, event_id=event_id, viewer_id=viewer.id)
    event = view.event
    organiser = view.is_organiser(viewer.id)
    if pending or event.attendee_visibility == AttendeeVisibility.organiser:
        if not organiser:
            raise ForbiddenError("Only the organiser can see that")
    elif event.attendee_visibility == AttendeeVisibility.attendees and not (
        organiser or (view.rsvp is not None and view.rsvp.is_accepted)
    ):
        raise ForbiddenError("Only people going can see who else is")

    statement = select(EventRsvp).where(
        EventRsvp.event_id == event.id,
        EventRsvp.approval == (RsvpApproval.pending if pending else RsvpApproval.approved),
        EventRsvp.response.is_not(None),
    )
    if response is not None:
        statement = statement.where(EventRsvp.response == response)
    hidden = await blocked_user_ids(session, viewer.id)
    if hidden:
        statement = statement.where(EventRsvp.user_id.not_in(hidden))
    return await paginate(
        session,
        statement,
        sort_column=EventRsvp.created_at,
        id_column=EventRsvp.id,
        params=params,
        descending=False,
    )


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BrowsePage:
    events: list[Event]
    next_cursor: str | None
    radius_km: float


def _upcoming(now: datetime) -> Any:
    return (
        Event.deleted_at.is_(None),
        Event.status == ContentStatus.visible,
        Event.event_status == EventStatus.scheduled,
        Event.end_at > now,
    )


async def browse(
    session: AsyncSession,
    redis: Redis,
    *,
    viewer: User,
    event_type: EventType | None,
    when: times.When,
    starts_from: datetime | None,
    starts_before: datetime | None,
    distance_km: float | None,
    mode: AnchorMode,
    params: PageParams,
    settings: Settings,
) -> BrowsePage:
    """Upcoming public events within the events radius, soonest first.

    `when` is "today" or "this_weekend" in local time; `starts_from` and
    `starts_before` (local, if no offset is given) narrow further. An event that
    has started but not ended still counts as on today.
    """
    anchor = await resolve_anchor(redis, user=viewer, mode=mode)
    if anchor is None:
        raise ConflictError(
            "Set your home area to see events nearby", details={"action": "PUT /v1/me/location"}
        )
    engine = RadiusEngine(session, redis, settings)
    radius = await engine.radius_for(RadiusFeature.events, anchor.cell_res8)
    radius_km = engine.effective_radius_km(radius, distance_km)

    now = datetime.now(UTC)
    zone = settings.event_zone
    window_start, window_end = times.window(when, now=now, zone=zone)
    statement = select(Event).where(
        *_upcoming(now),
        Event.visibility == EventVisibility.public,
        Event.end_at > window_start,
        func.ST_DWithin(
            Event.centroid, func.ST_GeogFromText(anchor.centroid_wkt), int(radius_km * 1000)
        ),
    )
    if window_end is not None:
        statement = statement.where(Event.start_at < window_end)
    if starts_from is not None:
        statement = statement.where(Event.start_at >= times.to_utc(starts_from, zone))
    if starts_before is not None:
        statement = statement.where(Event.start_at < times.to_utc(starts_before, zone))
    if event_type is not None:
        statement = statement.where(Event.event_type == event_type)
    hidden = await blocked_user_ids(session, viewer.id)
    if hidden:
        statement = statement.where(Event.organiser_id.not_in(hidden))
    events, next_cursor = await paginate(
        session,
        statement,
        sort_column=Event.start_at,
        id_column=Event.id,
        params=params,
        descending=False,
    )
    return BrowsePage(events=events, next_cursor=next_cursor, radius_km=radius_km)


async def group_events(
    session: AsyncSession, *, viewer: User, group_id: uuid.UUID, params: PageParams
) -> tuple[list[Event], str | None]:
    """A group's upcoming public and group-only events, for anyone who can read the
    group. A private event organised by a group is still private."""
    view = await group_access.readable_group(session, group_id=group_id, viewer_id=viewer.id)
    allowed = [EventVisibility.public]
    if view.is_active_member:
        allowed.append(EventVisibility.group)
    statement = select(Event).where(
        *_upcoming(datetime.now(UTC)),
        Event.group_id == group_id,
        Event.visibility.in_(allowed),
    )
    return await paginate(
        session,
        statement,
        sort_column=Event.start_at,
        id_column=Event.id,
        params=params,
        descending=False,
    )


async def my_events(
    session: AsyncSession, *, viewer: User, organising: bool, params: PageParams
) -> tuple[list[Event], str | None]:
    """Upcoming events the viewer organises, or has replied going or interested to."""
    now = datetime.now(UTC)
    statement = select(Event).where(
        Event.deleted_at.is_(None), Event.end_at > now, Event.status != ContentStatus.removed
    )
    if organising:
        statement = statement.where(Event.organiser_id == viewer.id)
    else:
        statement = statement.where(
            Event.id.in_(
                select(EventRsvp.event_id).where(
                    EventRsvp.user_id == viewer.id,
                    EventRsvp.response.in_((RsvpResponse.going, RsvpResponse.interested)),
                )
            )
        )
    return await paginate(
        session,
        statement,
        sort_column=Event.start_at,
        id_column=Event.id,
        params=params,
        descending=False,
    )


async def get_event(session: AsyncSession, *, viewer: User, event_id: uuid.UUID) -> EventView:
    return await access.visible_event(session, event_id=event_id, viewer_id=viewer.id)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReminderRun:
    sent: int
    events: int


async def send_event_reminders(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> ReminderRun:
    """Send every reminder due now. Safe to run as often as wanted.

    For each offset, the events due at it are those starting in
    `(now + next smaller offset, now + offset]` — exactly the events for which
    `times.reminder_due` answers this offset. It goes to people who were already
    going when it fell due. Each attendee's reminder is inserted into
    `event_reminders_sent` first; only the rows actually inserted are sent.
    """
    moment = now or datetime.now(UTC)
    offsets = settings.event_reminder_offset_list
    zone = settings.event_zone
    sent = 0
    seen: set[uuid.UUID] = set()
    for index, offset in enumerate(offsets):
        smaller = offsets[index + 1] if index + 1 < len(offsets) else 0
        due = (
            await session.execute(
                select(Event).where(
                    Event.deleted_at.is_(None),
                    Event.status == ContentStatus.visible,
                    Event.event_status == EventStatus.scheduled,
                    Event.start_at > moment + timedelta(minutes=smaller),
                    Event.start_at <= moment + timedelta(minutes=offset),
                )
            )
        ).scalars()
        for event in due:
            seen.add(event.id)
            # Only people already going when this reminder fell due. Someone who
            # replied after that has just looked at the time; reminding them
            # minutes later of what they just read is noise.
            due_moment = event.start_at - timedelta(minutes=offset)
            going = (
                (
                    await session.execute(
                        select(EventRsvp.user_id).where(
                            EventRsvp.event_id == event.id,
                            EventRsvp.response == RsvpResponse.going,
                            EventRsvp.approval == RsvpApproval.approved,
                            EventRsvp.updated_at <= due_moment,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not going:
                continue
            claimed = (
                (
                    await session.execute(
                        insert(EventReminderSent)
                        .values(
                            [
                                {
                                    "id": uuid.uuid4(),
                                    "event_id": event.id,
                                    "user_id": user_id,
                                    "offset_minutes": offset,
                                    "sent_at": moment,
                                }
                                for user_id in going
                            ]
                        )
                        .on_conflict_do_nothing(
                            constraint="uq_event_reminders_sent_event_id_user_id_offset_minutes"
                        )
                        .returning(EventReminderSent.user_id)
                    )
                )
                .scalars()
                .all()
            )
            when = times.describe_start(event.start_at, now=moment, zone=zone)
            for user_id in claimed:
                await notifications_service.notify(
                    session,
                    recipient_id=user_id,
                    kind=NotificationType.event_reminder,
                    actor_id=None,
                    target_type="event",
                    target_id=event.id,
                    collapse_key=f"event_reminder:{event.id}:{offset}",
                    data={"event_id": str(event.id), "offset_minutes": str(offset)},
                    push_text=f"{event.title} starts {when}",
                    settings=settings,
                )
                sent += 1
    if sent:
        logger.info("event_reminders_sent", extra={"count": sent, "events": len(seen)})
    return ReminderRun(sent=sent, events=len(seen))


async def remind_expiring_listings(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> int:
    """Tell sellers a day before a listing expires — carried over from Phase 5.

    `expiry_reminded_at` makes it once per term; renewing clears it.
    """
    moment = now or datetime.now(UTC)
    claimed = (
        await session.execute(
            update(Listing)
            .where(
                Listing.deleted_at.is_(None),
                Listing.status == ContentStatus.visible,
                Listing.availability != ListingAvailability.sold,
                Listing.expiry_reminded_at.is_(None),
                Listing.expires_at > moment,
                Listing.expires_at
                <= moment + timedelta(hours=settings.listing_expiry_reminder_hours),
            )
            .values(expiry_reminded_at=moment)
            .returning(Listing.id, Listing.seller_id, Listing.title)
        )
    ).all()
    for listing_id, seller_id, title in claimed:
        await notifications_service.notify(
            session,
            recipient_id=seller_id,
            kind=NotificationType.listing_expiring,
            actor_id=None,
            target_type="listing",
            target_id=listing_id,
            collapse_key=f"listing_expiring:{listing_id}",
            data={"listing_id": str(listing_id)},
            push_text=f"“{title}” expires soon. Renew it to keep it up.",
            settings=settings,
        )
    return len(claimed)


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def purge_user_events(
    session: AsyncSession, *, user_id: uuid.UUID, settings: Settings
) -> dict[str, int]:
    """Cancel and delete the events a purged account organised, and remove its
    replies. Attendees of a cancelled event are told, as for any cancellation."""
    now = datetime.now(UTC)
    organised = list(
        (
            await session.execute(
                select(Event).where(Event.organiser_id == user_id, Event.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    for event in organised:
        if event.event_status == EventStatus.scheduled and event.end_at > now:
            event.event_status = EventStatus.cancelled
            event.cancelled_at = now
            await session.flush()
            for recipient in await _rsvp_recipients(session, event):
                await notifications_service.notify(
                    session,
                    recipient_id=recipient,
                    kind=NotificationType.event_cancelled,
                    actor_id=None,
                    target_type="event",
                    target_id=event.id,
                    collapse_key=f"event_cancelled:{event.id}",
                    data={"event_id": str(event.id)},
                    settings=settings,
                )
        event.deleted_at = now
    replied_to = list(
        (await session.execute(select(EventRsvp.event_id).where(EventRsvp.user_id == user_id)))
        .scalars()
        .all()
    )
    removed = await execute_dml(session, delete(EventRsvp).where(EventRsvp.user_id == user_id))
    await execute_dml(
        session, delete(EventReminderSent).where(EventReminderSent.user_id == user_id)
    )
    for event_id in replied_to:
        await _recount(session, event_id)
    await session.flush()
    return {"events": len(organised), "rsvps": removed}


async def export_user_events(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    organised = (
        await session.execute(
            select(Event).where(Event.organiser_id == user_id).order_by(Event.start_at)
        )
    ).scalars()
    replies = (
        await session.execute(
            select(EventRsvp).where(EventRsvp.user_id == user_id).order_by(EventRsvp.created_at)
        )
    ).scalars()
    return {
        "organised": [
            {
                "id": str(event.id),
                "title": event.title,
                "type": str(event.event_type),
                "start_at": event.start_at.isoformat(),
                "end_at": event.end_at.isoformat(),
                "venue_name": event.venue_name,
                "venue_details": event.venue_details,
                "visibility": str(event.visibility),
                "status": str(event.event_status),
            }
            for event in organised
        ],
        "replies": [
            {
                "event_id": str(reply.event_id),
                "response": str(reply.response) if reply.response else None,
                "approval": str(reply.approval),
                "replied_at": reply.updated_at.isoformat(),
            }
            for reply in replies
        ],
    }
