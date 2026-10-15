"""Who may see an event, and who may see where it is. A leaf: models only.

**Seeing the event.**
* **Public:** anyone signed in.
* **Group:** the group's active members.
* **Private:** the organiser, and anyone with an RSVP row — invited, or arrived
  through the event's link.

Everyone else, and anyone blocked either way with the organiser, gets a 404.

**Seeing the venue** — the brief's location-privacy rule. A private event's venue
name and details go only to the organiser and to *accepted* attendees: an approved
RSVP of going or interested. An invitee who has not answered, someone waiting for
approval, and someone who said no all see the area label and nothing finer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.modules.events.models import Event, EventRsvp, EventVisibility
from app.modules.groups import access as group_access
from app.modules.groups.models import GroupMemberStatus
from app.modules.moderation.models import ContentStatus
from app.modules.users.service import is_blocked_between


def event_not_found() -> NotFoundError:
    return NotFoundError("Event not found")


@dataclass(frozen=True, slots=True)
class EventView:
    event: Event
    rsvp: EventRsvp | None

    def is_organiser(self, viewer_id: uuid.UUID) -> bool:
        return self.event.organiser_id == viewer_id

    def venue_visible(self, viewer_id: uuid.UUID) -> bool:
        if self.event.visibility != EventVisibility.private or self.is_organiser(viewer_id):
            return True
        return self.rsvp is not None and self.rsvp.is_accepted


async def rsvp_of(
    session: AsyncSession, event_id: uuid.UUID, user_id: uuid.UUID
) -> EventRsvp | None:
    return (
        await session.execute(
            select(EventRsvp)
            .where(EventRsvp.event_id == event_id, EventRsvp.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def load_event(session: AsyncSession, event_id: uuid.UUID, *, lock: bool = False) -> Event:
    statement = select(Event).where(Event.id == event_id, Event.deleted_at.is_(None))
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    event = (await session.execute(statement)).scalar_one_or_none()
    if event is None or event.status == ContentStatus.removed:
        raise event_not_found()
    return event


async def visible_event(
    session: AsyncSession, *, event_id: uuid.UUID, viewer_id: uuid.UUID, lock: bool = False
) -> EventView:
    event = await load_event(session, event_id, lock=lock)
    rsvp = await rsvp_of(session, event.id, viewer_id)
    view = EventView(event=event, rsvp=rsvp)
    if view.is_organiser(viewer_id):
        return view
    if event.status == ContentStatus.hidden_pending_review:
        raise event_not_found()
    if await is_blocked_between(session, viewer_id, event.organiser_id):
        raise event_not_found()
    if event.visibility == EventVisibility.group:
        assert event.group_id is not None  # noqa: S101 — the check constraint holds it
        member = await group_access.membership(session, event.group_id, viewer_id)
        if member is None or member.status != GroupMemberStatus.active:
            raise event_not_found()
    elif event.visibility == EventVisibility.private and rsvp is None:
        raise event_not_found()
    return view
