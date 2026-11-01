"""Who is in whose circle, for the modules that need to ask.

A leaf: the circle's models and nothing else, because the feed, messaging and
the block itself all ask, and the circle service imports some of them.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import execute_dml
from app.modules.circle.models import CircleLink, CircleRequest


async def in_circle(session: AsyncSession, first: uuid.UUID, second: uuid.UUID) -> bool:
    if first == second:
        return False
    low, high = CircleLink.pair(first, second)
    found = await session.scalar(
        select(CircleLink.user_low_id).where(
            CircleLink.user_low_id == low, CircleLink.user_high_id == high
        )
    )
    return found is not None


def other_member(user_id: uuid.UUID) -> Any:
    """For a link row involving `user_id`, the other person's ID, as SQL."""
    return case(
        (CircleLink.user_low_id == user_id, CircleLink.user_high_id),
        else_=CircleLink.user_low_id,
    )


def involving(user_id: uuid.UUID) -> Any:
    return or_(CircleLink.user_low_id == user_id, CircleLink.user_high_id == user_id)


def member_ids(user_id: uuid.UUID) -> Select[uuid.UUID]:
    """Everyone in `user_id`'s circle, as a subquery for `IN`."""
    return select(other_member(user_id)).where(involving(user_id))


async def circle_size(session: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(func.count()).select_from(CircleLink).where(involving(user_id)))
        or 0
    )


async def remove_all_between(session: AsyncSession, first: uuid.UUID, second: uuid.UUID) -> None:
    """A block ends everything between two people: the link and every request,
    both ways (decision C.2)."""
    low, high = CircleLink.pair(first, second)
    await execute_dml(
        session,
        delete(CircleLink).where(CircleLink.user_low_id == low, CircleLink.user_high_id == high),
    )
    await execute_dml(
        session,
        delete(CircleRequest).where(
            or_(
                (CircleRequest.requester_id == first) & (CircleRequest.recipient_id == second),
                (CircleRequest.requester_id == second) & (CircleRequest.recipient_id == first),
            )
        ),
    )
