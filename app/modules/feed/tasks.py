"""Background work for the feed: story expiry.

Stories promise to be gone in a day, and that promise is kept by deleting them,
not just by filtering them out of reads. Their images are personal data, so the
stored objects go too.

One exception: a story under a pending report is kept until a moderator decides.
Deleting it on schedule would destroy the evidence for exactly the stories most
likely to need it, and the brief is explicit that moderation needs evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.storage import StorageProvider
from app.modules.feed.models import Story
from app.modules.media.models import MediaAsset
from app.modules.moderation.models import Report, ReportStatus, ReportTargetType

logger = logging.getLogger(__name__)

EXPIRY_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class StoryExpiryResult:
    deleted: int
    held_for_review: int


async def expire_stories(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    now: datetime | None = None,
) -> StoryExpiryResult:
    """Delete expired stories and their images, except those awaiting review."""
    moment = now or datetime.now(UTC)

    under_review = exists().where(
        Report.target_type == ReportTargetType.story,
        Report.target_id == Story.id,
        Report.status == ReportStatus.pending,
    )

    expired = list(
        (
            await session.execute(
                select(Story)
                .where(Story.expires_at <= moment, ~under_review)
                .limit(EXPIRY_BATCH_SIZE)
            )
        )
        .scalars()
        .all()
    )
    held = await session.scalar(
        select(func.count(Story.id)).where(Story.expires_at <= moment, under_review)
    )

    media_ids = [story.media_id for story in expired if story.media_id is not None]
    for story in expired:
        await session.delete(story)
    # The story goes first: `stories.media_id` is RESTRICT, so the asset cannot be
    # removed while a story still points at it.
    await session.flush()

    if media_ids:
        assets = (
            await session.execute(select(MediaAsset).where(MediaAsset.id.in_(media_ids)))
        ).scalars()
        for asset in assets:
            for key in asset.variant_keys():
                await storage.delete_object(storage_key=key)
            if asset.original_key:
                await storage.delete_object(storage_key=asset.original_key)
            await session.delete(asset)
        await session.flush()

    if expired:
        logger.info("stories_expired", extra={"count": len(expired)})
    return StoryExpiryResult(deleted=len(expired), held_for_review=int(held or 0))
