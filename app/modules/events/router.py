"""Event endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.core.redis import get_redis
from app.modules.auth.dependencies import (
    ActiveUser,
    CurrentUser,
    tiered_user_rate_limit,
    user_rate_limit,
)
from app.modules.events import service as events_service
from app.modules.events import views
from app.modules.events.models import Event, EventType, RsvpResponse
from app.modules.events.schemas import (
    AttendeePageOut,
    CancelIn,
    EventBrowseOut,
    EventIn,
    EventOut,
    EventPageOut,
    EventUpdateIn,
    InvitedOut,
    InviteIn,
    LinkOut,
    ReviewRsvpIn,
    RsvpIn,
)
from app.modules.users.models import User

router = APIRouter(tags=["events"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]

_event_limit = tiered_user_rate_limit(
    bucket="create_event",
    window_seconds=86_400,
    new_account_limit=lambda s: s.new_account_events_per_day,
    established_limit=lambda s: s.events_per_day,
)
_change_limit = user_rate_limit(bucket="change_event", limit=200, window_seconds=3600)

NOT_FOUND_DOC = (
    "\n\nAn event the caller may not see — a private event they are not invited to, "
    "a group event outside their groups, or one from someone blocked — is a 404."
)


async def _one(
    session: SessionDep,
    redis: Redis,
    storage: StorageProvider,
    event: Event,
    viewer: User,
    settings: Settings,
) -> EventOut:
    (rendered,) = await views.event_views(
        session, redis, storage, events=[event], viewer=viewer, settings=settings
    )
    return rendered


async def _page(
    session: SessionDep,
    redis: Redis,
    storage: StorageProvider,
    events: list[Event],
    viewer: User,
    settings: Settings,
) -> list[EventOut]:
    return await views.event_views(
        session, redis, storage, events=events, viewer=viewer, settings=settings
    )


@router.post(
    "/events",
    response_model=EventOut,
    status_code=status.HTTP_201_CREATED,
    summary="Organise an event",
    description=(
        "Times without an offset are local time in `EVENT_TIMEZONE` (Africa/Nairobi). "
        "`venue` is the point picked on a map: it becomes a coarse cell for discovery "
        "and is discarded. `venue_name` and `venue_details` are the exact venue — for "
        "a **private** event, shown only to accepted attendees.\n\n"
        "With `group_id`, the event is organised for a group you are an admin of; "
        "`group` visibility makes it members-only. The organiser is counted as going."
    ),
    dependencies=[Depends(_event_limit)],
)
async def create_event(
    payload: EventIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> EventOut:
    event = await events_service.create_event(
        session, organiser=user, fields=payload.model_dump(), settings=settings
    )
    return await _one(session, redis, storage, event, user, settings)


@router.get(
    "/events",
    response_model=EventBrowseOut,
    summary="Upcoming public events nearby",
    description=(
        "Within the caller's events radius (twice the feed radius) or their own "
        "`distance_km` up to its cap, soonest first. `when`: `today` (until local "
        "midnight), `this_weekend` (local Saturday and Sunday), or `upcoming`. "
        "`starts_from` and `starts_before` narrow further; without an offset they "
        "are local time. Cancelled and finished events are left out."
    ),
)
async def browse(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
    event_type: EventType | None = None,
    when: Literal["upcoming", "today", "this_weekend"] = "upcoming",
    starts_from: datetime | None = None,
    starts_before: datetime | None = None,
    distance_km: Annotated[float | None, Query(gt=0, le=100)] = None,
    anchor: Annotated[Literal["home", "explore"], Query()] = "home",
) -> EventBrowseOut:
    result = await events_service.browse(
        session,
        redis,
        viewer=user,
        event_type=event_type,
        when=when,
        starts_from=starts_from,
        starts_before=starts_before,
        distance_km=distance_km,
        mode=anchor,
        params=page,
        settings=settings,
    )
    return EventBrowseOut(
        items=await _page(session, redis, storage, result.events, user, settings),
        next_cursor=result.next_cursor,
        radius_km=round(result.radius_km * 2) / 2,
    )


@router.get(
    "/events/mine",
    response_model=EventPageOut,
    summary="Events the caller is organising or going to",
    description=(
        "Upcoming, soonest first. `organising=false` lists events replied going or interested to."
    ),
)
async def my_events(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
    organising: bool = True,
) -> EventPageOut:
    events, next_cursor = await events_service.my_events(
        session, viewer=user, organising=organising, params=page
    )
    return EventPageOut(
        items=await _page(session, redis, storage, events, user, settings),
        next_cursor=next_cursor,
    )


@router.get(
    "/events/invite/{token}",
    response_model=EventOut,
    summary="Open a private event's link",
    description=(
        "The event a link leads to. Its venue stays hidden until the caller is an "
        "accepted attendee. Every way a link can be no good is the same 404."
    ),
)
async def preview_link(
    token: str,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> EventOut:
    event = await events_service.event_for_link(
        session, viewer=user, token=token, settings=settings
    )
    return await _one(session, redis, storage, event, user, settings)


@router.post(
    "/events/invite/{token}/rsvp",
    response_model=EventOut,
    summary="Reply through a private event's link",
    description="In an approval event, the reply waits for the organiser.",
    dependencies=[
        Depends(user_rate_limit(bucket="event_link_rsvp", limit=30, window_seconds=3600))
    ],
)
async def rsvp_with_link(
    token: str,
    payload: RsvpIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> EventOut:
    event = await events_service.event_for_link(
        session, viewer=user, token=token, settings=settings
    )
    view = await events_service.rsvp(
        session,
        user=user,
        event_id=event.id,
        response=payload.response,
        settings=settings,
        via_link=event,
    )
    return await _one(session, redis, storage, view.event, user, settings)


@router.get(
    "/events/{event_id}",
    response_model=EventOut,
    summary="Get an event",
    description="Cancelled events stay readable, marked as such." + NOT_FOUND_DOC,
)
async def get_event(
    event_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> EventOut:
    view = await events_service.get_event(session, viewer=user, event_id=event_id)
    return await _one(session, redis, storage, view.event, user, settings)


@router.patch(
    "/events/{event_id}",
    response_model=EventOut,
    summary="Change an event",
    description=(
        "The organiser, or an admin of the organising group. A new time or venue "
        "notifies everyone who replied, and reminders are re-timed."
    ),
    dependencies=[Depends(_change_limit)],
)
async def update_event(
    event_id: uuid.UUID,
    payload: EventUpdateIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> EventOut:
    event = await events_service.update_event(
        session,
        user=user,
        event_id=event_id,
        changes=payload.model_dump(exclude_unset=True),
        settings=settings,
    )
    return await _one(session, redis, storage, event, user, settings)


@router.post(
    "/events/{event_id}/cancel",
    response_model=EventOut,
    summary="Cancel an event",
    description="Everyone who replied is notified. Idempotent.",
    dependencies=[Depends(_change_limit)],
)
async def cancel_event(
    event_id: uuid.UUID,
    payload: CancelIn,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> EventOut:
    event = await events_service.cancel_event(
        session, user=user, event_id=event_id, reason=payload.reason, settings=settings
    )
    return await _one(session, redis, storage, event, user, settings)


@router.delete(
    "/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an event",
    description="An upcoming event is cancelled first, so everyone who replied is told.",
)
async def delete_event(
    event_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> None:
    await events_service.delete_event(session, user=user, event_id=event_id, settings=settings)


@router.put(
    "/events/{event_id}/rsvp",
    response_model=EventOut,
    summary="Reply to an event",
    description=(
        "`going`, `interested` or `not_going`. In an approval event a first reply "
        "waits for the organiser. `going` is refused with 409 when the event is full."
        + NOT_FOUND_DOC
    ),
    dependencies=[Depends(user_rate_limit(bucket="event_rsvp", limit=120, window_seconds=3600))],
)
async def rsvp(
    event_id: uuid.UUID,
    payload: RsvpIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> EventOut:
    view = await events_service.rsvp(
        session, user=user, event_id=event_id, response=payload.response, settings=settings
    )
    return await _one(session, redis, storage, view.event, user, settings)


@router.post(
    "/events/{event_id}/attendees/{user_id}/review",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Approve or decline a reply",
    description="The organiser, for events that approve attendees. Capacity is checked again.",
    dependencies=[Depends(_change_limit)],
)
async def review_rsvp(
    event_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: ReviewRsvpIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    await events_service.review_rsvp(
        session,
        user=user,
        event_id=event_id,
        attendee_id=user_id,
        approve=payload.action == "approve",
        settings=settings,
    )


@router.get(
    "/events/{event_id}/attendees",
    response_model=AttendeePageOut,
    summary="Who is going",
    description=(
        "As the event's attendee visibility allows: everyone who can see it, only "
        "accepted attendees, or only the organiser. `pending=true` lists replies "
        "waiting for approval, for the organiser."
    ),
)
async def attendees(
    event_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    page: PageDep,
    response: RsvpResponse | None = None,
    pending: bool = False,
) -> AttendeePageOut:
    rows, next_cursor = await events_service.attendees(
        session, viewer=user, event_id=event_id, response=response, pending=pending, params=page
    )
    return AttendeePageOut(
        items=await views.attendee_views(session, storage, rsvps=rows), next_cursor=next_cursor
    )


@router.post(
    "/events/{event_id}/invites",
    response_model=InvitedOut,
    summary="Invite people",
    description="The organiser. Invited people may reply without approval.",
    dependencies=[
        Depends(user_rate_limit(bucket="event_invite", limit=200, window_seconds=86_400))
    ],
)
async def invite(
    event_id: uuid.UUID,
    payload: InviteIn,
    user: ActiveUser,
    session: SessionDep,
    settings: SettingsDep,
) -> InvitedOut:
    invited = await events_service.invite(
        session, user=user, event_id=event_id, user_ids=payload.user_ids, settings=settings
    )
    return InvitedOut(invited=invited)


@router.post(
    "/events/{event_id}/link",
    response_model=LinkOut,
    status_code=status.HTTP_201_CREATED,
    summary="Make a link into a private event",
    description="Replaces any earlier link, which stops working.",
    dependencies=[Depends(_change_limit)],
)
async def create_link(
    event_id: uuid.UUID, user: ActiveUser, session: SessionDep, settings: SettingsDep
) -> LinkOut:
    token = await events_service.create_link(
        session, user=user, event_id=event_id, settings=settings
    )
    return LinkOut(token=token)


@router.delete(
    "/events/{event_id}/link",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop a private event's link working",
    description="People who already replied through it keep their replies.",
)
async def revoke_link(event_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await events_service.revoke_link(session, user=user, event_id=event_id)


@router.get(
    "/groups/{group_id}/events",
    response_model=EventPageOut,
    summary="A group's upcoming events",
    description="Public events for anyone who can read the group; group-only ones for its members.",
    tags=["groups"],
)
async def group_events(
    group_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
) -> EventPageOut:
    events, next_cursor = await events_service.group_events(
        session, viewer=user, group_id=group_id, params=page
    )
    return EventPageOut(
        items=await _page(session, redis, storage, events, user, settings),
        next_cursor=next_cursor,
    )
