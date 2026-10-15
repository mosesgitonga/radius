"""Turning events into responses, in batches — and applying the venue rule."""

from __future__ import annotations

from collections.abc import Sequence

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.storage import StorageProvider
from app.modules.events import times
from app.modules.events.access import EventView
from app.modules.events.models import Event, EventRsvp
from app.modules.events.schemas import AttendeeOut, EventOut, MyRsvpOut
from app.modules.feed.views import author_view, load_assets, load_users
from app.modules.location import cells
from app.modules.location.service import area_label_for_cell
from app.modules.media.views import image_variants
from app.modules.users.models import User
from app.modules.users.schemas import AreaLabelOut


async def event_views(
    session: AsyncSession,
    redis: Redis,
    storage: StorageProvider,
    *,
    events: Sequence[Event],
    viewer: User,
    settings: Settings,
) -> list[EventOut]:
    if not events:
        return []
    ids = [event.id for event in events]
    mine = {
        rsvp.event_id: rsvp
        for rsvp in (
            await session.execute(
                select(EventRsvp).where(EventRsvp.event_id.in_(ids), EventRsvp.user_id == viewer.id)
            )
        ).scalars()
    }
    organisers = await load_users(session, (event.organiser_id for event in events))
    assets = await load_assets(
        session,
        [event.cover_media_id for event in events]
        + [user.avatar_media_id for user in organisers.values()],
    )
    labels: dict[int, AreaLabelOut] = {}
    for cell in {event.cell_res9 for event in events}:
        label = await area_label_for_cell(session, redis, cell_res9=cell, settings=settings)
        labels[cell] = AreaLabelOut(label=label.label, precision=label.precision)

    zone = settings.event_zone
    views: list[EventOut] = []
    for event in events:
        organiser = organisers.get(event.organiser_id)
        if organiser is None:  # pragma: no cover
            continue
        rsvp = mine.get(event.id)
        venue_visible = EventView(event=event, rsvp=rsvp).venue_visible(viewer.id)
        is_organiser = event.organiser_id == viewer.id
        bucket = None
        if not is_organiser and viewer.home_cell_res8 is not None:
            bucket = cells.bucket_between_cells(viewer.home_cell_res8, event.cell_res8)
        views.append(
            EventOut(
                id=event.id,
                title=event.title,
                description=event.description,
                event_type=event.event_type,
                start_at=event.start_at,
                end_at=event.end_at,
                start_local=times.to_local(event.start_at, zone),
                end_local=times.to_local(event.end_at, zone),
                timezone=settings.event_timezone,
                organiser=author_view(organiser, assets, storage),
                group_id=event.group_id,
                area_label=labels[event.cell_res9],
                distance_bucket=bucket,
                venue_visible=venue_visible,
                venue_name=event.venue_name if venue_visible else None,
                venue_details=event.venue_details if venue_visible else None,
                cover=image_variants(
                    assets.get(event.cover_media_id) if event.cover_media_id else None, storage
                ),
                visibility=event.visibility,
                requires_approval=event.requires_approval,
                attendee_visibility=event.attendee_visibility,
                capacity=event.capacity,
                going_count=event.going_count,
                interested_count=event.interested_count,
                is_full=event.capacity is not None and event.going_count >= event.capacity,
                event_status=event.event_status,
                cancellation_reason=event.cancellation_reason,
                my_rsvp=(
                    MyRsvpOut(response=rsvp.response, approval=rsvp.approval) if rsvp else None
                ),
                is_organiser=is_organiser,
                status=event.status,
                created_at=event.created_at,
            )
        )
    return views


async def attendee_views(
    session: AsyncSession, storage: StorageProvider, *, rsvps: Sequence[EventRsvp]
) -> list[AttendeeOut]:
    users = await load_users(session, (rsvp.user_id for rsvp in rsvps))
    assets = await load_assets(session, [user.avatar_media_id for user in users.values()])
    return [
        AttendeeOut(
            user=author_view(users[rsvp.user_id], assets, storage),
            response=rsvp.response,
            approval=rsvp.approval,
        )
        for rsvp in rsvps
        if rsvp.user_id in users
    ]
