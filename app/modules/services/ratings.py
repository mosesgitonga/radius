"""The cached rating on each user. A leaf, so moderation can refresh it after
auto-hiding a review without importing the services module.

`users.rating_average` and `users.rating_count` are derived from visible,
undeleted reviews about that user, recomputed in one statement whenever the set
changes — never incremented, so a missed event cannot leave the cache drifting.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.moderation.models import ContentStatus
from app.modules.services.models import Review
from app.modules.users.models import User


async def recompute(session: AsyncSession, subject_id: uuid.UUID) -> None:
    conditions = (
        Review.subject_id == subject_id,
        Review.deleted_at.is_(None),
        Review.status == ContentStatus.visible,
    )
    # With no reviews, AVG is NULL — which is what "no rating yet" should be.
    average = select(func.round(func.avg(Review.rating), 2)).where(*conditions)
    count = select(func.count(Review.id)).where(*conditions)
    await session.execute(
        update(User)
        .where(User.id == subject_id)
        .values(rating_average=average.scalar_subquery(), rating_count=count.scalar_subquery())
    )


async def recompute_for_review(session: AsyncSession, review_id: uuid.UUID) -> None:
    subject_id = await session.scalar(select(Review.subject_id).where(Review.id == review_id))
    if subject_id is not None:
        await recompute(session, subject_id)
