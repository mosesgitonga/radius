"""Following a service.

A follow reaches across any distance: it lasts wherever the follower and the
provider live or move to, and posts made as the service reach the follower
from anywhere (decision C.5). Nobody learns who follows them, only how many.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import Select, delete, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, NotFoundError, RateLimitedError
from app.core.pagination import PageParams, paginate
from app.core.ratelimit import get_rate_limiter
from app.modules.follows.models import Follow, FollowEntityType
from app.modules.moderation.models import ContentStatus
from app.modules.services.models import ServiceKind, ServicePost
from app.modules.users.models import User
from app.modules.users.service import blocked_user_ids, is_blocked_between

logger = logging.getLogger(__name__)


def _not_found() -> NotFoundError:
    return NotFoundError("Service not found")


async def _followable_service(
    session: AsyncSession, *, viewer: User, entity_id: uuid.UUID
) -> ServicePost:
    """A live offering someone else provides, or a 404 that does not say why."""
    post = await session.scalar(
        select(ServicePost).where(
            ServicePost.id == entity_id,
            ServicePost.kind == ServiceKind.offering,
            ServicePost.deleted_at.is_(None),
            ServicePost.status == ContentStatus.visible,
        )
    )
    if post is None or await is_blocked_between(session, viewer.id, post.author_id):
        raise _not_found()
    if post.author_id == viewer.id:
        raise BadRequestError("You cannot follow your own service")
    return post


async def _find(
    session: AsyncSession,
    *,
    follower_id: uuid.UUID,
    entity_type: FollowEntityType,
    entity_id: uuid.UUID,
) -> Follow | None:
    return await session.scalar(
        select(Follow).where(
            Follow.follower_id == follower_id,
            Follow.entity_type == entity_type,
            Follow.entity_id == entity_id,
        )
    )


async def follow(
    session: AsyncSession,
    *,
    user: User,
    entity_type: FollowEntityType,
    entity_id: uuid.UUID,
    settings: Settings,
) -> Follow:
    """Follow an entity. Idempotent: following again returns the same follow."""
    await _followable_service(session, viewer=user, entity_id=entity_id)
    existing = await _find(
        session, follower_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is not None:
        return existing

    result = await get_rate_limiter().hit(
        "follow", f"u:{user.id}", limit=settings.follows_per_day, window_seconds=86_400
    )
    if not result.allowed:
        logger.info("rate_limited", extra={"bucket": "follow", "scope": "user"})
        raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)

    record = Follow(follower_id=user.id, entity_type=entity_type, entity_id=entity_id)
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError:
        # The same follow from two taps at once; the other insert won.
        found = await _find(
            session, follower_id=user.id, entity_type=entity_type, entity_id=entity_id
        )
        if found is None:  # pragma: no cover — the unique constraint guarantees a row
            raise
        return found
    return record


async def unfollow(
    session: AsyncSession, *, user: User, entity_type: FollowEntityType, entity_id: uuid.UUID
) -> None:
    """Idempotent: unfollowing what you do not follow is not an error."""
    await execute_dml(
        session,
        delete(Follow).where(
            Follow.follower_id == user.id,
            Follow.entity_type == entity_type,
            Follow.entity_id == entity_id,
        ),
    )


def _live_services() -> Any:
    return exists().where(
        ServicePost.id == Follow.entity_id,
        ServicePost.deleted_at.is_(None),
        ServicePost.status == ContentStatus.visible,
    )


async def my_follows(
    session: AsyncSession, *, user: User, params: PageParams
) -> tuple[list[Follow], str | None]:
    """What I follow, newest first. A service that has gone, been taken down, or
    whose provider is blocked either way is left out."""
    blocked = await blocked_user_ids(session, user.id)
    statement: Select[Follow] = select(Follow).where(
        Follow.follower_id == user.id,
        Follow.entity_type == FollowEntityType.service_provider,
        _live_services(),
    )
    if blocked:
        statement = statement.where(
            ~exists().where(ServicePost.id == Follow.entity_id, ServicePost.author_id.in_(blocked))
        )
    return await paginate(
        session, statement, sort_column=Follow.created_at, id_column=Follow.id, params=params
    )


# ---------------------------------------------------------------------------
# Retention and data rights
# ---------------------------------------------------------------------------
async def delete_orphaned_follows(session: AsyncSession) -> int:
    """Follows of services that have been deleted. Run daily by the retention job."""
    removed = await execute_dml(
        session,
        delete(Follow).where(
            Follow.entity_type == FollowEntityType.service_provider,
            ~exists().where(ServicePost.id == Follow.entity_id, ServicePost.deleted_at.is_(None)),
        ),
    )
    if removed:
        logger.info("orphaned_follows_deleted", extra={"count": removed})
    return removed


async def purge_user_follows(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Everything the person follows. Follows *of* their services go when the
    services do, through `delete_orphaned_follows`."""
    return await execute_dml(session, delete(Follow).where(Follow.follower_id == user_id))


async def export_user_follows(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Follow).where(Follow.follower_id == user_id).order_by(Follow.created_at)
        )
    ).scalars()
    return {
        "following": [
            {
                "entity_type": str(row.entity_type),
                "entity_id": str(row.entity_id),
                "followed_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "notes": (
            "Who follows your services is not shown to you or anyone else; only how many people do."
        ),
    }
