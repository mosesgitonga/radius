"""Retention: what is finally deleted, and when.

Deletion everywhere else in the product is soft, so that a report keeps its
evidence and an appeal has something to look at. This is where soft becomes hard:

* **Content** deleted by its author, or removed by a moderator, more than
  `CONTENT_PURGE_AFTER_DAYS` ago is deleted outright, with its images — unless a
  report about it, or about anything the deletion would cascade to, is still
  pending. A group's deletion cascades to its posts and events, and a post's to
  its comments, so those reports hold it too.
* **Messages** are the exception: a deleted message stays as a "message deleted"
  tombstone, because conversations are ordered by sequence number and a gap would
  confuse every client's sync. What goes is the content kept for a moderator.
* **The audit log, resolved reports and moderator warnings** are kept for
  `AUDIT_RETENTION_DAYS` (two years by default), then deleted. The audit log is
  append-only in every other respect; this is the one deletion it allows.

Each sweep is batched, so one night's run is bounded however much has built up.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.core.providers.storage import StorageProvider
from app.modules.events.models import Event
from app.modules.feed.models import Comment, Post, PostImage, Story
from app.modules.groups.models import Group
from app.modules.marketplace.models import Listing, ListingImage
from app.modules.media.models import MediaAsset
from app.modules.messaging.models import Message
from app.modules.moderation.models import (
    AuditEvent,
    ContentStatus,
    ModerationWarning,
    Report,
    ReportStatus,
    ReportTargetType,
)
from app.modules.services.models import Review, ServicePost
from app.modules.users.models import Consent, User

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000


def _pending(target_type: ReportTargetType, target_id: Any) -> ColumnElement[bool]:
    return exists().where(
        Report.target_type == target_type,
        Report.target_id == target_id,
        Report.status == ReportStatus.pending,
    )


def _due(model: Any, cutoff: datetime) -> ColumnElement[bool]:
    """Deleted by its author before the cutoff, or removed by a moderator before it."""
    return or_(
        model.deleted_at < cutoff,
        and_(model.status == ContentStatus.removed, model.hidden_at < cutoff),
    )


async def _ids(session: AsyncSession, statement: Any) -> list[uuid.UUID]:
    return list((await session.execute(statement.limit(BATCH_SIZE))).scalars())


async def _media_of_posts(session: AsyncSession, post_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if not post_ids:
        return []
    statement = select(PostImage.media_id).where(PostImage.post_id.in_(post_ids))
    return list((await session.execute(statement)).scalars())


async def _delete_unreferenced_media(
    session: AsyncSession, storage: StorageProvider, media_ids: Iterable[uuid.UUID | None]
) -> int:
    """Delete assets, objects and all, once nothing points at them any more."""
    wanted = {media_id for media_id in media_ids if media_id is not None}
    if not wanted:
        return 0
    referenced = or_(
        exists().where(PostImage.media_id == MediaAsset.id),
        exists().where(ListingImage.media_id == MediaAsset.id),
        exists().where(Story.media_id == MediaAsset.id),
        exists().where(Message.media_id == MediaAsset.id),
        exists().where(Event.cover_media_id == MediaAsset.id),
        exists().where(Group.cover_media_id == MediaAsset.id),
        exists().where(User.avatar_media_id == MediaAsset.id),
    )
    assets = list(
        (
            await session.execute(select(MediaAsset).where(MediaAsset.id.in_(wanted), ~referenced))
        ).scalars()
    )
    for asset in assets:
        keys = asset.variant_keys()
        if asset.original_key:
            keys.append(asset.original_key)
        for key in keys:
            await storage.delete_object(storage_key=key)
        await session.delete(asset)
    await session.flush()
    return len(assets)


async def purge_old_content(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, int]:
    """Hard-delete content past its retention, with its images."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.content_purge_after_days)
    counts: dict[str, int] = {}
    media: list[uuid.UUID | None] = []

    # Groups first: deleting one cascades to its posts and events.
    group_post_ids = select(Post.id).where(Post.group_id == Group.id)
    group_event_ids = select(Event.id).where(Event.group_id == Group.id)
    group_ids = await _ids(
        session,
        select(Group.id).where(
            Group.deleted_at < cutoff,
            ~_pending(ReportTargetType.group, Group.id),
            ~exists().where(
                Report.target_type == ReportTargetType.post,
                Report.target_id.in_(group_post_ids.scalar_subquery()),
                Report.status == ReportStatus.pending,
            ),
            ~exists().where(
                Report.target_type == ReportTargetType.event,
                Report.target_id.in_(group_event_ids.scalar_subquery()),
                Report.status == ReportStatus.pending,
            ),
        ),
    )
    if group_ids:
        cascaded_posts = list(
            (await session.execute(select(Post.id).where(Post.group_id.in_(group_ids)))).scalars()
        )
        media += await _media_of_posts(session, cascaded_posts)
        media += list(
            (
                await session.execute(
                    select(Event.cover_media_id).where(Event.group_id.in_(group_ids))
                )
            ).scalars()
        )
        media += list(
            (
                await session.execute(select(Group.cover_media_id).where(Group.id.in_(group_ids)))
            ).scalars()
        )
    counts["groups"] = await execute_dml(session, delete(Group).where(Group.id.in_(group_ids)))

    # Posts: a pending report on one of its comments holds it too.
    comment_report = exists().where(
        Report.target_type == ReportTargetType.comment,
        Report.target_id.in_(
            select(Comment.id).where(Comment.post_id == Post.id).scalar_subquery()
        ),
        Report.status == ReportStatus.pending,
    )
    post_ids = await _ids(
        session,
        select(Post.id).where(
            _due(Post, cutoff), ~_pending(ReportTargetType.post, Post.id), ~comment_report
        ),
    )
    media += await _media_of_posts(session, post_ids)
    counts["posts"] = await execute_dml(session, delete(Post).where(Post.id.in_(post_ids)))

    comment_ids = await _ids(
        session,
        select(Comment.id).where(
            _due(Comment, cutoff), ~_pending(ReportTargetType.comment, Comment.id)
        ),
    )
    counts["comments"] = await execute_dml(
        session, delete(Comment).where(Comment.id.in_(comment_ids))
    )

    story_rows = (
        await session.execute(
            select(Story.id, Story.media_id)
            .where(_due(Story, cutoff), ~_pending(ReportTargetType.story, Story.id))
            .limit(BATCH_SIZE)
        )
    ).all()
    media += [row.media_id for row in story_rows]
    counts["stories"] = await execute_dml(
        session, delete(Story).where(Story.id.in_([row.id for row in story_rows]))
    )

    listing_ids = await _ids(
        session,
        select(Listing.id).where(
            _due(Listing, cutoff), ~_pending(ReportTargetType.listing, Listing.id)
        ),
    )
    if listing_ids:
        media += list(
            (
                await session.execute(
                    select(ListingImage.media_id).where(ListingImage.listing_id.in_(listing_ids))
                )
            ).scalars()
        )
    counts["listings"] = await execute_dml(
        session, delete(Listing).where(Listing.id.in_(listing_ids))
    )

    service_ids = await _ids(
        session,
        select(ServicePost.id).where(
            _due(ServicePost, cutoff), ~_pending(ReportTargetType.service_post, ServicePost.id)
        ),
    )
    counts["service_posts"] = await execute_dml(
        session, delete(ServicePost).where(ServicePost.id.in_(service_ids))
    )

    event_rows = (
        await session.execute(
            select(Event.id, Event.cover_media_id)
            .where(_due(Event, cutoff), ~_pending(ReportTargetType.event, Event.id))
            .limit(BATCH_SIZE)
        )
    ).all()
    media += [row.cover_media_id for row in event_rows]
    counts["events"] = await execute_dml(
        session, delete(Event).where(Event.id.in_([row.id for row in event_rows]))
    )

    review_ids = await _ids(
        session,
        select(Review.id).where(
            _due(Review, cutoff), ~_pending(ReportTargetType.review, Review.id)
        ),
    )
    counts["reviews"] = await execute_dml(session, delete(Review).where(Review.id.in_(review_ids)))

    # Messages keep their tombstone; the evidence kept for a moderator goes.
    message_rows = (
        await session.execute(
            select(Message.id, Message.media_id)
            .where(
                Message.deleted_at < cutoff,
                or_(Message.body.is_not(None), Message.media_id.is_not(None)),
                ~_pending(ReportTargetType.message, Message.id),
            )
            .limit(BATCH_SIZE)
        )
    ).all()
    media += [row.media_id for row in message_rows]
    counts["message_contents_erased"] = await execute_dml(
        session,
        update(Message)
        .where(Message.id.in_([row.id for row in message_rows]))
        .values(body=None, media_id=None),
    )

    await session.flush()
    counts["media"] = await _delete_unreferenced_media(session, storage, media)
    if any(counts.values()):
        logger.info("content_purged", extra=counts)
    return counts


async def purge_old_records(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> dict[str, int]:
    """Audit events, resolved reports and warnings past the retention period."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.audit_retention_days)
    counts = {
        "audit_events": await execute_dml(
            session, delete(AuditEvent).where(AuditEvent.created_at < cutoff)
        ),
        "resolved_reports": await execute_dml(
            session,
            delete(Report).where(
                Report.status != ReportStatus.pending, Report.resolved_at < cutoff
            ),
        ),
        "warnings": await execute_dml(
            session, delete(ModerationWarning).where(ModerationWarning.created_at < cutoff)
        ),
        "consents": await purge_consents_of_purged_accounts(session, settings=settings, now=now),
    }
    if any(counts.values()):
        logger.info("records_purged", extra=counts)
    return counts


async def purge_consents_of_purged_accounts(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> int:
    """Consent records are the evidence of what someone agreed to, so they outlive
    the account by the audit retention period, and no longer."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.audit_retention_days)
    purged_long_ago = select(User.id).where(User.deleted_at < cutoff)
    return await execute_dml(session, delete(Consent).where(Consent.user_id.in_(purged_long_ago)))


# ---------------------------------------------------------------------------
# One person's moderation data
# ---------------------------------------------------------------------------
async def purge_user_moderation(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """At account purge: the warnings they received. The audit log keeps that a
    warning was given; the wording goes."""
    return await execute_dml(
        session, delete(ModerationWarning).where(ModerationWarning.user_id == user_id)
    )


async def export_user_moderation(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    """For the data export: warnings received and reports filed. Never who
    reported *them* — that is someone else's data, and the reason reporting works."""
    warnings = (
        await session.execute(
            select(ModerationWarning)
            .where(ModerationWarning.user_id == user_id)
            .order_by(ModerationWarning.created_at)
        )
    ).scalars()
    reports = (
        await session.execute(
            select(Report).where(Report.reporter_id == user_id).order_by(Report.created_at)
        )
    ).scalars()
    return {
        "warnings_received": [
            {"reason": warning.reason, "given_at": warning.created_at.isoformat()}
            for warning in warnings
        ],
        "reports_filed": [
            {
                "target_type": str(report.target_type),
                "target_id": str(report.target_id),
                "reason": str(report.reason),
                "details": report.details,
                "status": str(report.status),
                "filed_at": report.created_at.isoformat(),
            }
            for report in reports
        ],
    }
