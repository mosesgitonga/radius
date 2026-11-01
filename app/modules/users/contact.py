"""Whether someone may approach people they do not know yet.

Home is found by GPS, and a GPS reading can be faked. So someone new to an area
may post, comment and react at once, but may not start a conversation with a
stranger, or invite one into a group chat, a group or an event, until they have
been at home on `CONTACT_UNLOCK_DAYS` of the last `CONTACT_UNLOCK_WINDOW_DAYS` days
(decision M.7).
Verified neighbours are always unlocked. Moving home locks it again until the new
area is proven.

A leaf: models, settings and errors only, because messaging, groups, events and
the location service all ask.
"""

from __future__ import annotations

from datetime import date, datetime

from app.core.config import Settings
from app.core.errors import ForbiddenError
from app.modules.users.models import User

CONTACT_LOCKED = "contact_locked"


def presence_days(user: User, today: date, *, window_days: int) -> int:
    """Days at home among the last `window_days`, today included.

    Bit 0 of the mask is the day it was last updated, and higher bits are older
    days. A mask not yet rolled to today still says the same about the days it
    covers; it simply covers fewer of the window.
    """
    if user.home_presence_updated_on is None:
        return 0
    elapsed = (today - user.home_presence_updated_on).days
    if elapsed < 0 or elapsed >= window_days:
        return 0
    covered = window_days - elapsed
    return bin(user.home_presence_mask & ((1 << covered) - 1)).count("1")


def days_this_week(user: User, *, settings: Settings, today: date) -> int:
    return presence_days(user, today, window_days=settings.contact_unlock_window_days)


def can_contact_strangers(user: User) -> bool:
    return user.is_verified_neighbour or user.contact_unlocked_at is not None


def note_presence(user: User, *, settings: Settings, now: datetime) -> None:
    """After a day's presence is recorded: unlock once there are enough days."""
    if (
        user.contact_unlocked_at is None
        and days_this_week(user, settings=settings, today=now.date())
        >= settings.contact_unlock_days
    ):
        user.contact_unlocked_at = now


def require_can_contact_strangers(user: User, *, settings: Settings, now: datetime) -> None:
    if can_contact_strangers(user):
        return
    needed = settings.contact_unlock_days
    window = settings.contact_unlock_window_days
    so_far = min(days_this_week(user, settings=settings, today=now.date()), needed)
    raise ForbiddenError(
        f"You can contact people you don't know yet once you've been at home in your area on "
        f"{needed} of the last {window} days. So far: {so_far} of {needed}.",
        details={
            "reason": CONTACT_LOCKED,
            "days_needed": needed,
            "window_days": window,
            "days_so_far": so_far,
        },
    )
