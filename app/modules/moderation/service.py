"""Moderation service: reports, and the auto-hide rule.

Phase 8 adds the queue, moderator actions, and the admin console. Phase 2 brings
the part every content module needs first: a way for neighbours to flag
something, and a rule for hiding it before a human gets there.

The audit writer lives in `moderation/audit.py`, not here — see that module for
why it has to be a leaf.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, NotFoundError
from app.modules.events import access as events_access
from app.modules.events.models import Event
from app.modules.feed import service as feed_service
from app.modules.feed.models import Comment, Post, Story
from app.modules.groups import access as group_access
from app.modules.marketplace import access as listing_access
from app.modules.marketplace.models import Listing
from app.modules.messaging.models import ConversationMember, MemberStatus, Message
from app.modules.moderation.audit import record_audit_event
from app.modules.moderation.models import (
    REPORT_PRIORITY,
    AuditAction,
    AuditActorType,
    ContentStatus,
    Report,
    ReportReason,
    ReportStatus,
    ReportTargetType,
)
from app.modules.services import access as services_access
from app.modules.services import ratings
from app.modules.services.models import Review, ServicePost
from app.modules.users.models import User, UserStatus
from app.modules.users.service import get_user, is_blocked_between

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReportOutcome:
    report: Report
    created: bool
    auto_hidden: bool
    """Not returned to the reporter — see `ReportOut` for why."""


async def is_trusted_reporter(session: AsyncSession, *, user: User, settings: Settings) -> bool:
    """Whether a reporter's report counts towards auto-hiding.

    Phase 2's stand-in for the verified badge, which arrives in Phase 8: an
    account old enough, with a verified phone, in good standing, with nothing
    upheld against it. Each condition closes a specific abuse:

    * **Age** — otherwise a burst of fresh accounts can hide anything.
    * **Phone verified** — every account is today, but the check costs nothing
      and stays correct if verification ever becomes optional.
    * **Active status** — a suspended account should not be steering moderation.
    * **Nothing upheld against them** — somebody found to be a bad actor should
      not be able to silence others.

    Untrusted reports still enter the queue for a human. They just cannot hide
    anything on their own.
    """
    if user.status != UserStatus.active or user.phone_verified_at is None:
        return False
    if user.is_verified_neighbour:
        return True

    age = datetime.now(UTC) - user.created_at
    if age < timedelta(days=settings.trusted_reporter_min_account_age_days):
        return False

    upheld_against = await session.scalar(
        select(func.count(Report.id)).where(
            Report.target_type == ReportTargetType.user,
            Report.target_id == user.id,
            Report.status == ReportStatus.upheld,
        )
    )
    return not upheld_against


async def submit_report(
    session: AsyncSession,
    *,
    reporter: User,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    reason: ReportReason,
    details: str | None,
    settings: Settings,
) -> ReportOutcome:
    """File a report, and hide the target if enough trusted neighbours agree.

    One report per person per target. A repeat returns the original rather than
    erroring — the reporter's intent is already recorded — and it cannot push the
    count up, which is what stops one person reaching the threshold alone.
    """
    target_author = await _resolve_target_author(
        session, viewer=reporter, target_type=target_type, target_id=target_id
    )
    if target_author == reporter.id:
        raise BadRequestError("You cannot report yourself or your own content")

    existing = await _find_report(
        session, reporter_id=reporter.id, target_type=target_type, target_id=target_id
    )
    if existing is not None:
        return ReportOutcome(report=existing, created=False, auto_hidden=False)

    trusted = await is_trusted_reporter(session, user=reporter, settings=settings)
    report = Report(
        reporter_id=reporter.id,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        details=(details or "").strip() or None,
        priority=REPORT_PRIORITY.get(reason, 0),
        counted_towards_auto_hide=trusted,
    )
    try:
        async with session.begin_nested():
            session.add(report)
    except IntegrityError:
        # A double tap: the unique index caught the second. Return the first.
        existing = await _find_report(
            session, reporter_id=reporter.id, target_type=target_type, target_id=target_id
        )
        if existing is None:  # pragma: no cover — the index guarantees a row
            raise
        return ReportOutcome(report=existing, created=False, auto_hidden=False)

    auto_hidden = False
    if trusted and target_type in _HIDEABLE:
        auto_hidden = await _maybe_auto_hide(
            session, target_type=target_type, target_id=target_id, settings=settings
        )

    await record_audit_event(
        session,
        action=AuditAction.report_submitted,
        actor_id=reporter.id,
        target_type=str(target_type),
        target_id=target_id,
        context={"reason": str(reason), "trusted": trusted, "auto_hidden": auto_hidden},
    )
    logger.info(
        "report_submitted",
        extra={"reason": str(reason), "target_type": str(target_type), "trusted": trusted},
    )
    return ReportOutcome(report=report, created=True, auto_hidden=auto_hidden)


async def _find_report(
    session: AsyncSession,
    *,
    reporter_id: uuid.UUID,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
) -> Report | None:
    return (
        await session.execute(
            select(Report).where(
                Report.reporter_id == reporter_id,
                Report.target_type == target_type,
                Report.target_id == target_id,
            )
        )
    ).scalar_one_or_none()


# Users are deliberately absent. Auto-hiding *content* is reversible and narrow;
# auto-suspending a *person* on three reports would hand a coordinated group a
# way to silence a neighbour. Reports against users always wait for a human.
_HIDEABLE: dict[ReportTargetType, Any] = {
    ReportTargetType.post: Post,
    ReportTargetType.listing: Listing,
    ReportTargetType.event: Event,
    ReportTargetType.service_post: ServicePost,
    ReportTargetType.review: Review,
    ReportTargetType.comment: Comment,
    ReportTargetType.story: Story,
}


async def _maybe_auto_hide(
    session: AsyncSession,
    *,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    settings: Settings,
) -> bool:
    """Hide content once enough distinct trusted reporters have flagged it.

    Distinctness is guaranteed by the unique index, so a count of rows is a count
    of people. Only `visible` content moves: what a moderator removed stays
    removed, and what is already hidden is not hidden twice.
    """
    trusted_reports = await session.scalar(
        select(func.count(Report.id)).where(
            Report.target_type == target_type,
            Report.target_id == target_id,
            Report.counted_towards_auto_hide.is_(True),
            Report.status == ReportStatus.pending,
        )
    )
    if (trusted_reports or 0) < settings.report_auto_hide_threshold:
        return False

    model = _HIDEABLE[target_type]
    hidden = await execute_dml(
        session,
        update(model)
        .where(model.id == target_id, model.status == ContentStatus.visible)
        .values(status=ContentStatus.hidden_pending_review, hidden_at=datetime.now(UTC)),
    )
    if hidden and target_type == ReportTargetType.review:
        # A hidden review no longer counts towards its subject's rating.
        await ratings.recompute_for_review(session, target_id)
    if hidden:
        await record_audit_event(
            session,
            action=AuditAction.content_auto_hidden,
            actor_type=AuditActorType.system,
            target_type=str(target_type),
            target_id=target_id,
            context={"trusted_reports": int(trusted_reports or 0)},
        )
        logger.info("content_auto_hidden", extra={"target_type": str(target_type)})
    return bool(hidden)


async def _resolve_target_author(
    session: AsyncSession,
    *,
    viewer: User,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
) -> uuid.UUID:
    """The author of a reportable thing, after checking the viewer can see it.

    Reporting goes through the same visibility rules as reading. If somebody could
    report content they cannot see, the report endpoint would become a way to probe
    for hidden or blocked content — and a block would not really be one.
    """
    if target_type == ReportTargetType.post:
        post = await feed_service.get_post(session, viewer=viewer, post_id=target_id)
        return post.author_id
    if target_type == ReportTargetType.story:
        story = await feed_service.get_story(session, viewer=viewer, story_id=target_id)
        return story.author_id
    if target_type == ReportTargetType.comment:
        comment = (
            await session.execute(
                select(Comment).where(Comment.id == target_id, Comment.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if comment is None:
            raise NotFoundError("Comment not found")
        # Seeing a comment requires seeing its post, and not being blocked.
        await feed_service.get_post(session, viewer=viewer, post_id=comment.post_id)
        if await is_blocked_between(session, viewer.id, comment.author_id):
            raise NotFoundError("Comment not found")
        return comment.author_id
    if target_type == ReportTargetType.message:
        # Only a member who can see the message may report it. Models, not the
        # messaging service, because messaging imports this module.
        message = (
            await session.execute(
                select(Message)
                .join(
                    ConversationMember,
                    (ConversationMember.conversation_id == Message.conversation_id)
                    & (ConversationMember.user_id == viewer.id),
                )
                .where(
                    Message.id == target_id,
                    Message.deleted_at.is_(None),
                    Message.sender_id.is_not(None),
                    ConversationMember.status == MemberStatus.active,
                    Message.seq > ConversationMember.visible_from_seq,
                )
            )
        ).scalar_one_or_none()
        if message is None or message.sender_id is None:
            raise NotFoundError("Message not found")
        if await is_blocked_between(session, viewer.id, message.sender_id):
            raise NotFoundError("Message not found")
        return message.sender_id
    if target_type == ReportTargetType.event:
        event_view = await events_access.visible_event(
            session, event_id=target_id, viewer_id=viewer.id
        )
        return event_view.event.organiser_id
    if target_type == ReportTargetType.service_post:
        service_post = await services_access.visible_post(
            session, post_id=target_id, viewer_id=viewer.id
        )
        return service_post.author_id
    if target_type == ReportTargetType.review:
        review = await services_access.visible_review(
            session, review_id=target_id, viewer_id=viewer.id
        )
        return review.reviewer_id
    if target_type == ReportTargetType.listing:
        listing = await listing_access.visible_listing(
            session, listing_id=target_id, viewer_id=viewer.id
        )
        return listing.seller_id
    if target_type == ReportTargetType.group:
        # Anyone who can see a group's profile may report it; the owner answers
        # for it. A group being reported is not auto-hidden: hiding a whole
        # community on reports alone is a decision for a person.
        view = await group_access.visible_group(session, group_id=target_id, viewer_id=viewer.id)
        if view.group.owner_id is None:
            raise NotFoundError("Group not found")
        return view.group.owner_id
    if target_type == ReportTargetType.user:
        target = await get_user(session, target_id)
        if target is None or await is_blocked_between(session, viewer.id, target_id):
            # The same rule as every other read: blocked means invisible.
            raise NotFoundError("User not found")
        return target.id
    raise BadRequestError(
        "That kind of content cannot be reported yet",
        details={"target_type": str(target_type)},
    )
