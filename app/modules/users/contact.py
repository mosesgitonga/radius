"""Whether someone may approach people they do not know yet.

Home is found by GPS, and a GPS reading can be faked. So someone new to an area
may post, comment and react at once, but may not start a conversation with a
stranger — or invite one into a group chat, a group or an event — until they have
been at home on `CONTACT_UNLOCK_DAYS` days in a row (decision M.7). Verified
neighbours are always unlocked. Moving home locks it again until the new area is
proven.

A leaf: models, settings and errors only, because messaging, groups, events and
the location service all ask.
"""

from __future__ import annotations

from datetime import date, datetime

from app.core.config import Settings
from app.core.errors import ForbiddenError
from app.modules.users.models import User

CONTACT_LOCKED = "contact_locked"


def presence_streak(user: User, today: date) -> int:
    """Days in a row at home, counting back from the most recent recorded day.

    Bit 0 of the mask is the day it was last updated. A mask last updated today or
    yesterday still describes an unbroken run — yesterday's check-in is not undone
    by today's not having happened yet. Anything older has been broken.
    """
    if user.home_presence_updated_on is None:
        return 0
    if (today - user.home_presence_updated_on).days > 1:
        return 0
    mask = user.home_presence_mask
    streak = 0
    while mask & 1:
        streak += 1
        mask >>= 1
    return streak


def can_contact_strangers(user: User) -> bool:
    return user.is_verified_neighbour or user.contact_unlocked_at is not None


def note_presence(user: User, *, settings: Settings, now: datetime) -> None:
    """After a day's presence is recorded: unlock once the run is long enough."""
    if (
        user.contact_unlocked_at is None
        and presence_streak(user, now.date()) >= settings.contact_unlock_days
    ):
        user.contact_unlocked_at = now


def require_can_contact_strangers(user: User, *, settings: Settings, now: datetime) -> None:
    if can_contact_strangers(user):
        return
    needed = settings.contact_unlock_days
    so_far = min(presence_streak(user, now.date()), needed)
    raise ForbiddenError(
        f"You can start conversations with people once you've been at home in your area on "
        f"{needed} days in a row. You're on day {so_far} of {needed}.",
        details={"reason": CONTACT_LOCKED, "days_needed": needed, "days_so_far": so_far},
    )
