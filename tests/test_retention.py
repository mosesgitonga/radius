"""Retention: soft-deleted content and old records are finally deleted, and
nothing under a pending report is."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.storage import MemoryStorageProvider
from app.modules.feed.models import Comment, Post
from app.modules.media.models import MediaAsset
from app.modules.messaging.models import (
    Conversation,
    ConversationKind,
    Message,
    MessageKind,
    direct_key_for,
)
from app.modules.moderation import retention
from app.modules.moderation.models import (
    AuditAction,
    AuditActorType,
    AuditEvent,
    ContentStatus,
    Report,
    ReportReason,
    ReportStatus,
    ReportTargetType,
)
from app.modules.users.models import Consent, ConsentType, User
from tests.factories import NYERI_TOWN, make_post, make_ready_image, make_user


def _long_ago(settings: Settings, extra_days: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(days=settings.content_purge_after_days + extra_days)


async def _exists(db_session: AsyncSession, model: Any, row_id: uuid.UUID) -> bool:
    found = await db_session.scalar(
        select(func.count()).select_from(model).where(model.id == row_id)
    )
    return bool(found)


async def _pending_report(
    db_session: AsyncSession, target_type: ReportTargetType, target_id: uuid.UUID
) -> None:
    reporter = await make_user(db_session, at=NYERI_TOWN)
    db_session.add(
        Report(
            reporter_id=reporter.id,
            target_type=target_type,
            target_id=target_id,
            reason=ReportReason.spam,
        )
    )
    await db_session.flush()


async def test_old_deleted_posts_go_with_their_images(
    db_session: AsyncSession,
    redis_client: Redis,
    storage: MemoryStorageProvider,
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    image = await make_ready_image(db_session, storage, owner=author)
    image_id, keys = image.id, image.variant_keys()
    post = await make_post(db_session, redis_client, author=author, media_ids=[image_id])
    post.deleted_at = _long_ago(settings)
    await db_session.flush()
    assert keys
    assert all([await storage.object_exists(storage_key=key) for key in keys])

    counts = await retention.purge_old_content(db_session, storage, settings=settings)
    assert counts["posts"] == 1
    assert counts["media"] == 1
    assert not await _exists(db_session, Post, post.id)
    assert not await _exists(db_session, MediaAsset, image_id)
    assert not any([await storage.object_exists(storage_key=key) for key in keys])


async def test_recently_deleted_and_live_content_stays(
    db_session: AsyncSession,
    redis_client: Redis,
    storage: MemoryStorageProvider,
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    recent = await make_post(db_session, redis_client, author=author)
    recent.deleted_at = datetime.now(UTC) - timedelta(days=settings.content_purge_after_days - 1)
    live = await make_post(db_session, redis_client, author=author)
    await db_session.flush()
    counts = await retention.purge_old_content(db_session, storage, settings=settings)
    assert counts["posts"] == 0
    assert await _exists(db_session, Post, recent.id)
    assert await _exists(db_session, Post, live.id)


async def test_old_removed_content_goes_too(
    db_session: AsyncSession,
    redis_client: Redis,
    storage: MemoryStorageProvider,
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    post.status = ContentStatus.removed
    post.hidden_at = _long_ago(settings)
    await db_session.flush()
    await retention.purge_old_content(db_session, storage, settings=settings)
    assert not await _exists(db_session, Post, post.id)


async def test_a_pending_report_holds_content_and_what_it_cascades_to(
    db_session: AsyncSession,
    redis_client: Redis,
    storage: MemoryStorageProvider,
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reported = await make_post(db_session, redis_client, author=author)
    parent = await make_post(db_session, redis_client, author=author)
    comment = Comment(post_id=parent.id, author_id=author.id, body="Reported reply")
    db_session.add(comment)
    await db_session.flush()
    for post in (reported, parent):
        post.deleted_at = _long_ago(settings)
    await db_session.flush()
    await _pending_report(db_session, ReportTargetType.post, reported.id)
    await _pending_report(db_session, ReportTargetType.comment, comment.id)

    counts = await retention.purge_old_content(db_session, storage, settings=settings)
    assert counts["posts"] == 0
    assert await _exists(db_session, Post, reported.id)
    # Deleting the parent would cascade to the reported comment.
    assert await _exists(db_session, Post, parent.id)
    assert await _exists(db_session, Comment, comment.id)


async def test_deleted_messages_keep_their_tombstone(
    db_session: AsyncSession, storage: MemoryStorageProvider, settings: Settings
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    other = await make_user(db_session, at=NYERI_TOWN)
    conversation = Conversation(
        kind=ConversationKind.direct,
        created_by_id=sender.id,
        direct_key=direct_key_for(sender.id, other.id),
    )
    db_session.add(conversation)
    await db_session.flush()
    message = Message(
        conversation_id=conversation.id,
        sender_id=sender.id,
        seq=1,
        kind=MessageKind.text,
        body="Evidence kept for a moderator",
        deleted_at=_long_ago(settings),
    )
    db_session.add(message)
    await db_session.flush()

    counts = await retention.purge_old_content(db_session, storage, settings=settings)
    assert counts["message_contents_erased"] == 1
    await db_session.refresh(message)
    assert message.body is None
    assert message.deleted_at is not None


async def test_records_older_than_two_years_go(
    db_session: AsyncSession, settings: Settings
) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=settings.audit_retention_days + 1)
    reporter = await make_user(db_session, at=NYERI_TOWN)
    old_event = AuditEvent(
        actor_type=AuditActorType.system,
        action=AuditAction.badge_awarded,
        target_type="user",
        context={},
        created_at=long_ago,
    )
    recent_event = AuditEvent(
        actor_type=AuditActorType.system, action=AuditAction.badge_awarded, context={}
    )
    resolved = Report(
        reporter_id=reporter.id,
        target_type=ReportTargetType.user,
        target_id=uuid.uuid4(),
        reason=ReportReason.spam,
        status=ReportStatus.dismissed,
        resolved_at=long_ago,
    )
    pending = Report(
        reporter_id=reporter.id,
        target_type=ReportTargetType.user,
        target_id=uuid.uuid4(),
        reason=ReportReason.spam,
    )
    db_session.add_all([old_event, recent_event, resolved, pending])
    await db_session.flush()

    counts = await retention.purge_old_records(db_session, settings=settings)
    assert counts["audit_events"] == 1
    assert counts["resolved_reports"] == 1
    assert await _exists(db_session, AuditEvent, recent_event.id)
    assert await _exists(db_session, Report, pending.id)


async def test_consents_outlive_the_account_by_two_years(
    db_session: AsyncSession, settings: Settings
) -> None:
    async def purged_user(days_ago: int) -> User:
        user = await make_user(db_session, at=NYERI_TOWN)
        db_session.add(
            Consent(
                user_id=user.id,
                consent_type=ConsentType.terms,
                granted=True,
                document_version="2026-01-01",
            )
        )
        user.deleted_at = datetime.now(UTC) - timedelta(days=days_ago)
        await db_session.flush()
        return user

    old = await purged_user(settings.audit_retention_days + 1)
    recent = await purged_user(30)
    removed = await retention.purge_consents_of_purged_accounts(db_session, settings=settings)
    assert removed == 1
    remaining = set(
        (
            await db_session.execute(
                select(Consent.user_id).where(Consent.user_id.in_([old.id, recent.id]))
            )
        ).scalars()
    )
    assert remaining == {recent.id}
