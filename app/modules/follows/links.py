"""Follow counts and lookups, for the modules that show or use follows.

A leaf: the follows model only, so the feed and the services views can ask
without importing the follows service.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.follows.models import Follow, FollowEntityType


def followed_ids(follower_id: uuid.UUID, entity_type: FollowEntityType) -> Select[uuid.UUID]:
    """The entities of one type someone follows, as a subquery for `IN`."""
    return select(Follow.entity_id).where(
        Follow.follower_id == follower_id, Follow.entity_type == entity_type
    )


async def follower_counts(
    session: AsyncSession, entity_type: FollowEntityType, entity_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not entity_ids:
        return {}
    rows = await session.execute(
        select(Follow.entity_id, func.count())
        .where(Follow.entity_type == entity_type, Follow.entity_id.in_(entity_ids))
        .group_by(Follow.entity_id)
    )
    return {entity_id: int(count) for entity_id, count in rows.all()}


async def following(
    session: AsyncSession,
    follower_id: uuid.UUID,
    entity_type: FollowEntityType,
    entity_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    if not entity_ids:
        return set()
    rows = await session.scalars(
        followed_ids(follower_id, entity_type).where(Follow.entity_id.in_(entity_ids))
    )
    return set(rows)
