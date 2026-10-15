"""Local time for events: reading it, showing it, wording it, and bounding it.

Every function takes the zone and "now" explicitly. Nothing here reads the clock,
which is what lets the reminder tests pin "now" to a Friday evening in Nairobi
and assert exactly what is due.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Literal

When = Literal["upcoming", "today", "this_weekend"]


def to_utc(value: datetime, zone: tzinfo) -> datetime:
    """An instant from what a person sent. A time with no offset is local wall time
    in `zone` — "Saturday 18:00" means 18:00 where the event is."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(UTC)


def to_local(value: datetime, zone: tzinfo) -> datetime:
    return value.astimezone(zone)


def window(when: When, *, now: datetime, zone: tzinfo) -> tuple[datetime, datetime | None]:
    """The [start, end) span a date filter covers, in UTC.

    * **today**: from now until local midnight.
    * **this_weekend**: local Saturday 00:00 until Monday 00:00 — or from now, if it
      is already the weekend.
    * **upcoming**: from now, with no end.
    """
    local_now = to_local(now, zone)
    if when == "upcoming":
        return now, None
    if when == "today":
        midnight = datetime.combine(local_now.date() + timedelta(days=1), time(0), tzinfo=zone)
        return now, midnight.astimezone(UTC)
    # Saturday is weekday 5, Sunday 6.
    days_to_saturday = (5 - local_now.weekday()) % 7
    if local_now.weekday() == 6:
        days_to_saturday = -1
    saturday = datetime.combine(
        local_now.date() + timedelta(days=days_to_saturday), time(0), tzinfo=zone
    )
    monday = saturday + timedelta(days=2)
    start = max(saturday.astimezone(UTC), now)
    return start, monday.astimezone(UTC)


def describe_start(start_at: datetime, *, now: datetime, zone: tzinfo) -> str:
    """How a reminder says when: "today at 18:00", "tomorrow at 09:30", or "on Sat
    17 Oct at 18:00". Twenty-four-hour clock, as Kenyan phones commonly show."""
    start = to_local(start_at, zone)
    today = to_local(now, zone).date()
    clock = start.strftime("%H:%M")
    if start.date() == today:
        return f"today at {clock}"
    if start.date() == today + timedelta(days=1):
        return f"tomorrow at {clock}"
    return f"on {start.strftime('%a')} {start.day} {start.strftime('%b')} at {clock}"


def reminder_due(offsets: tuple[int, ...], *, start_at: datetime, now: datetime) -> int | None:
    """Which reminder, if any, is due now for an event starting at `start_at`.

    `offsets` are minutes, largest first. An offset is due once its moment has
    passed, and stops being due when the next, smaller one is — so a worker that
    was down for a while sends the reminder that still makes sense, not a stale
    one. Who receives it is decided by the sweep: only people already going when
    it fell due.
    """
    if now >= start_at:
        return None
    for index, offset in enumerate(offsets):
        due_from = start_at - timedelta(minutes=offset)
        smaller = offsets[index + 1] if index + 1 < len(offsets) else 0
        due_until = start_at - timedelta(minutes=smaller)
        if due_from <= now < due_until:
            return offset
    return None
