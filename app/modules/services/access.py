"""Who may see a service post or a review. A leaf: models only, so moderation can
use it without an import cycle.

The post rule: readable by anyone signed in, by link, open or closed. Held for
review — the job-scam check, or reports — is seen only by its author; removed by
a moderator, by nobody. Anyone blocked either way is a 404.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.modules.moderation.models import ContentStatus
from app.modules.services.models import Review, ServicePost
from app.modules.users.service import is_blocked_between


def post_not_found() -> NotFoundError:
    return NotFoundError("Post not found")


async def visible_post(
    session: AsyncSession, *, post_id: uuid.UUID, viewer_id: uuid.UUID, lock: bool = False
) -> ServicePost:
    statement = select(ServicePost).where(
        ServicePost.id == post_id, ServicePost.deleted_at.is_(None)
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    post = (await session.execute(statement)).scalar_one_or_none()
    if post is None or post.status == ContentStatus.removed:
        raise post_not_found()
    mine = post.author_id == viewer_id
    if post.status == ContentStatus.hidden_pending_review and not mine:
        raise post_not_found()
    if not mine and await is_blocked_between(session, viewer_id, post.author_id):
        raise post_not_found()
    return post


async def own_post(
    session: AsyncSession, *, post_id: uuid.UUID, author_id: uuid.UUID
) -> ServicePost:
    post = await visible_post(session, post_id=post_id, viewer_id=author_id, lock=True)
    if post.author_id != author_id:
        raise ForbiddenError("Only the author can change this post")
    return post


def review_not_found() -> NotFoundError:
    return NotFoundError("Review not found")


async def visible_review(
    session: AsyncSession, *, review_id: uuid.UUID, viewer_id: uuid.UUID, lock: bool = False
) -> Review:
    statement = select(Review).where(Review.id == review_id, Review.deleted_at.is_(None))
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    review = (await session.execute(statement)).scalar_one_or_none()
    if review is None or review.status == ContentStatus.removed:
        raise review_not_found()
    involved = viewer_id in (review.reviewer_id, review.subject_id)
    if review.status == ContentStatus.hidden_pending_review and viewer_id != review.reviewer_id:
        raise review_not_found()
    if not involved and (
        await is_blocked_between(session, viewer_id, review.reviewer_id)
        or await is_blocked_between(session, viewer_id, review.subject_id)
    ):
        raise review_not_found()
    return review
