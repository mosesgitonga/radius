"""An account's standing: restricted or suspended, right now.

A leaf, models only, because everything that creates content asks.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.modules.users.models import User, UserStatus


def is_restricted(user: User, now: datetime | None = None) -> bool:
    """Held to new-account limits, no first-contact messages or job posts, and new
    posts held for review. Lapses on its own at `restricted_until`."""
    return user.restricted_until is not None and user.restricted_until > (now or datetime.now(UTC))


def is_suspended(user: User, now: datetime | None = None) -> bool:
    """Suspended and not yet due to lift. A suspension past its end counts as
    lifted even before the sweep has reset the status."""
    if user.status != UserStatus.suspended:
        return False
    return user.suspended_until is None or user.suspended_until > (now or datetime.now(UTC))
