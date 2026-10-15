"""The moderation console: the queue, and what moderators do about it.

**The queue** is one list of things needing a human, worst first:

* everything with a pending report, grouped by what was reported, ranked by the
  worst reason against it (violence, then job scams, then the rest — the bands in
  `REPORT_PRIORITY`), then by how long it has waited;
* content held without a report — a job post held for a fee demand, a post from a
  restricted account — ranked alongside, so nothing held waits invisibly.

**Actions** change content or accounts, resolve the reports behind them, tell the
person affected, and write an audit row with actor type `moderator`. A moderator
can remove and restore content, warn, suspend, restrict, and manage badges;
banning, and the ban lists, are for admins.

Moderators see what was reported in full — that is the job — but no more of a
person than the job needs: no phone number, no location, no cell.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Select,
    String,
    and_,
    case,
    cast,
    delete,
    func,
    literal,
    or_,
    select,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, ConflictError, InvalidCursorError, NotFoundError
from app.modules.auth import service as auth_service
from app.modules.auth.models import DeviceSession
from app.modules.events.models import Event
from app.modules.feed.models import Comment, Post, PostImage, Story
from app.modules.groups import service as groups_service
from app.modules.groups.models import Group
from app.modules.marketplace.models import Listing, ListingImage
from app.modules.messaging import service as messaging_service
from app.modules.messaging.models import Message
from app.modules.moderation import banlist
from app.modules.moderation.audit import record_audit_event
from app.modules.moderation.models import (
    REPORT_PRIORITY,
    AuditAction,
    AuditActorType,
    AuditEvent,
    BannedDevice,
    BannedPhone,
    ContentStatus,
    ModerationWarning,
    Report,
    ReportReason,
    ReportStatus,
    ReportTargetType,
)
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.services import ratings
from app.modules.services.models import Review, ServicePost
from app.modules.staff.models import StaffAccount
from app.modules.users.models import User, UserStatus

logger = logging.getLogger(__name__)

# Content with a status column: hidden by reports, held, removed, restored.
STATUS_MODELS: dict[ReportTargetType, Any] = {
    ReportTargetType.post: Post,
    ReportTargetType.comment: Comment,
    ReportTargetType.story: Story,
    ReportTargetType.listing: Listing,
    ReportTargetType.service_post: ServicePost,
    ReportTargetType.event: Event,
    ReportTargetType.review: Review,
}
AUTHOR_COLUMN: dict[ReportTargetType, str] = {
    ReportTargetType.post: "author_id",
    ReportTargetType.comment: "author_id",
    ReportTargetType.story: "author_id",
    ReportTargetType.listing: "seller_id",
    ReportTargetType.service_post: "author_id",
    ReportTargetType.event: "organiser_id",
    ReportTargetType.review: "reviewer_id",
    ReportTargetType.message: "sender_id",
    ReportTargetType.group: "owner_id",
}
ALL_MODELS: dict[ReportTargetType, Any] = {
    **STATUS_MODELS,
    ReportTargetType.message: Message,
    ReportTargetType.group: Group,
    ReportTargetType.user: User,
}

# Held content with no report: a job post held for a fee demand ranks with job-scam
# reports; anything else held (a restricted account's post) sits mid-queue.
HELD_JOB_SCAM_PRIORITY = REPORT_PRIORITY[ReportReason.job_scam]
HELD_PRIORITY = 45

EXCERPT_LENGTH = 280


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QueueItem:
    target_type: ReportTargetType
    target_id: uuid.UUID
    priority: int
    waiting_since: datetime
    report_count: int
    trusted_report_count: int
    reasons: list[str] = field(default_factory=list)


def _held_rows(target_type: ReportTargetType, model: Any) -> Select[Any]:
    priority: Any = literal(HELD_PRIORITY)
    if model is ServicePost:
        priority = case(
            (func.jsonb_array_length(ServicePost.flagged_phrases) > 0, HELD_JOB_SCAM_PRIORITY),
            else_=HELD_PRIORITY,
        )
    return select(
        literal(target_type.value, String).label("target_type"),
        model.id.label("target_id"),
        priority.label("priority"),
        func.coalesce(model.hidden_at, model.updated_at).label("since"),
        literal(0).label("reports"),
        literal(0).label("trusted"),
    ).where(model.status == ContentStatus.hidden_pending_review, model.deleted_at.is_(None))


def _encode_queue_cursor(item: QueueItem) -> str:
    payload = {"p": item.priority, "s": item.waiting_since.isoformat(), "i": str(item.target_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_queue_cursor(cursor: str) -> tuple[int, datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode())
        payload = json.loads(raw)
        return int(payload["p"]), datetime.fromisoformat(payload["s"]), uuid.UUID(payload["i"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidCursorError from exc


async def queue(
    session: AsyncSession,
    *,
    limit: int,
    cursor: str | None,
    target_type: ReportTargetType | None = None,
) -> tuple[list[QueueItem], str | None]:
    """One page of the queue: worst first, then longest waiting."""
    reported = (
        select(
            cast(Report.target_type, String).label("target_type"),
            Report.target_id.label("target_id"),
            func.max(Report.priority).label("priority"),
            func.min(Report.created_at).label("since"),
            func.count(Report.id).label("reports"),
            func.count(Report.id)
            .filter(Report.counted_towards_auto_hide.is_(True))
            .label("trusted"),
        )
        .where(Report.status == ReportStatus.pending)
        .group_by(Report.target_type, Report.target_id)
    )
    parts: list[Any] = [reported]
    for kind, model in STATUS_MODELS.items():
        parts.append(_held_rows(kind, model))
    combined = union_all(*parts).subquery("combined")
    grouped = (
        select(
            combined.c.target_type,
            combined.c.target_id,
            func.max(combined.c.priority).label("priority"),
            func.min(combined.c.since).label("since"),
            func.sum(combined.c.reports).label("reports"),
            func.sum(combined.c.trusted).label("trusted"),
        )
        .group_by(combined.c.target_type, combined.c.target_id)
        .subquery("grouped")
    )
    statement = select(grouped)
    if target_type is not None:
        statement = statement.where(grouped.c.target_type == target_type.value)
    if cursor:
        priority, since, target_id = _decode_queue_cursor(cursor)
        statement = statement.where(
            or_(
                grouped.c.priority < priority,
                and_(
                    grouped.c.priority == priority,
                    or_(
                        grouped.c.since > since,
                        and_(grouped.c.since == since, grouped.c.target_id > target_id),
                    ),
                ),
            )
        )
    statement = statement.order_by(
        grouped.c.priority.desc(), grouped.c.since.asc(), grouped.c.target_id.asc()
    ).limit(limit + 1)
    rows = (await session.execute(statement)).all()
    items = [
        QueueItem(
            target_type=ReportTargetType(row.target_type),
            target_id=row.target_id,
            priority=int(row.priority),
            waiting_since=row.since,
            report_count=int(row.reports or 0),
            trusted_report_count=int(row.trusted or 0),
        )
        for row in rows[:limit]
    ]
    await _attach_reasons(session, items)
    next_cursor = _encode_queue_cursor(items[-1]) if len(rows) > limit and items else None
    return items, next_cursor


async def _attach_reasons(session: AsyncSession, items: list[QueueItem]) -> None:
    if not items:
        return
    rows = await session.execute(
        select(Report.target_type, Report.target_id, Report.reason)
        .where(
            Report.status == ReportStatus.pending,
            Report.target_id.in_([item.target_id for item in items]),
        )
        .distinct()
    )
    by_target: dict[tuple[str, uuid.UUID], set[ReportReason]] = {}
    for kind, target_id, reason in rows:
        by_target.setdefault((str(kind), target_id), set()).add(ReportReason(reason))
    for item in items:
        reasons = by_target.get((str(item.target_type), item.target_id), set())
        ranked = sorted(reasons, key=lambda reason: -REPORT_PRIORITY.get(reason, 0))
        item.reasons.extend(str(reason) for reason in ranked)


# ---------------------------------------------------------------------------
# What was reported
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TargetView:
    target_type: ReportTargetType
    target_id: uuid.UUID
    author_id: uuid.UUID | None
    status: str
    """`visible`, `hidden_pending_review`, `removed` or `deleted`."""
    text: str
    created_at: datetime | None
    media_ids: list[uuid.UUID]
    details: dict[str, Any]


def _content_text(target_type: ReportTargetType, row: Any) -> tuple[str, dict[str, Any]]:
    match target_type:
        case ReportTargetType.post | ReportTargetType.comment:
            return str(row.body), {}
        case ReportTargetType.story:
            return row.text or "", {}
        case ReportTargetType.listing:
            return f"{row.title}\n\n{row.description}", {"price_kes": row.price_kes}
        case ReportTargetType.service_post:
            return (
                f"{row.title}\n\n{row.description}",
                {"kind": str(row.kind), "flagged_phrases": list(row.flagged_phrases)},
            )
        case ReportTargetType.event:
            return f"{row.title}\n\n{row.description}", {"start_at": row.start_at.isoformat()}
        case ReportTargetType.review:
            return str(row.body), {"rating": row.rating, "subject_id": str(row.subject_id)}
        case ReportTargetType.message:
            return row.body or "", {"conversation_id": str(row.conversation_id)}
        case ReportTargetType.group:
            return f"{row.name}\n\n{row.description}", {}
        case _:
            return str(row.display_name), {"status": str(row.status)}


_SINGLE_MEDIA: dict[ReportTargetType, str] = {
    ReportTargetType.story: "media_id",
    ReportTargetType.message: "media_id",
    ReportTargetType.event: "cover_media_id",
    ReportTargetType.group: "cover_media_id",
    ReportTargetType.user: "avatar_media_id",
}


async def _media_ids(
    session: AsyncSession, target_type: ReportTargetType, row: Any
) -> list[uuid.UUID]:
    if target_type == ReportTargetType.post:
        statement: Select[Any] = (
            select(PostImage.media_id)
            .where(PostImage.post_id == row.id)
            .order_by(PostImage.position)
        )
        return list((await session.execute(statement)).scalars())
    if target_type == ReportTargetType.listing:
        statement = (
            select(ListingImage.media_id)
            .where(ListingImage.listing_id == row.id)
            .order_by(ListingImage.position)
        )
        return list((await session.execute(statement)).scalars())
    column = _SINGLE_MEDIA.get(target_type)
    value = getattr(row, column, None) if column else None
    return [value] if value is not None else []


async def _load_target(
    session: AsyncSession,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Any:
    model = ALL_MODELS[target_type]
    row = await session.get(model, target_id, with_for_update=lock)
    if row is None:
        raise NotFoundError("Nothing with that ID")
    return row


async def view_target(
    session: AsyncSession, target_type: ReportTargetType, target_id: uuid.UUID
) -> TargetView:
    row = await _load_target(session, target_type, target_id)
    text, details = _content_text(target_type, row)
    if getattr(row, "deleted_at", None) is not None:
        status = "deleted"
    elif target_type in STATUS_MODELS:
        status = str(row.status)
    else:
        status = "visible"
    author_column = AUTHOR_COLUMN.get(target_type)
    return TargetView(
        target_type=target_type,
        target_id=target_id,
        author_id=getattr(row, author_column) if author_column else row.id,
        status=status,
        text=text,
        created_at=getattr(row, "created_at", None),
        media_ids=await _media_ids(session, target_type, row),
        details=details,
    )


async def reports_for(
    session: AsyncSession, target_type: ReportTargetType, target_id: uuid.UUID
) -> list[Report]:
    statement = (
        select(Report)
        .where(Report.target_type == target_type, Report.target_id == target_id)
        .order_by(Report.created_at.desc())
        .limit(200)
    )
    return list((await session.execute(statement)).scalars())


# ---------------------------------------------------------------------------
# Acting on content
# ---------------------------------------------------------------------------
async def _resolve_pending(
    session: AsyncSession,
    *,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    outcome: ReportStatus,
    staff: StaffAccount,
    note: str | None,
) -> int:
    return await execute_dml(
        session,
        update(Report)
        .where(
            Report.target_type == target_type,
            Report.target_id == target_id,
            Report.status == ReportStatus.pending,
        )
        .values(
            status=outcome,
            resolved_at=datetime.now(UTC),
            resolved_by_id=staff.id,
            resolution_note=note,
        ),
    )


async def _tell(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    notice: str,
    target_type: str,
    target_id: uuid.UUID,
    text: str,
    settings: Settings,
) -> None:
    """An account notice: in the app, and pushed with the wording."""
    if user_id is None:
        return
    await notifications_service.notify(
        session,
        recipient_id=user_id,
        kind=NotificationType.account_notice,
        actor_id=None,
        target_type=target_type,
        target_id=target_id,
        collapse_key=f"account_notice:{notice}:{target_id}",
        settings=settings,
        data={"notice": notice},
        push_text=text,
    )


_NOUN: dict[ReportTargetType, str] = {
    ReportTargetType.post: "post",
    ReportTargetType.comment: "comment",
    ReportTargetType.story: "story",
    ReportTargetType.listing: "listing",
    ReportTargetType.service_post: "post",
    ReportTargetType.event: "event",
    ReportTargetType.review: "review",
    ReportTargetType.message: "message",
    ReportTargetType.group: "group",
}


async def remove_content(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    reason: str,
    settings: Settings,
) -> int:
    """Take content down and uphold the reports against it. Returns how many
    reports were resolved."""
    if target_type == ReportTargetType.user:
        raise BadRequestError("A person is not content: warn, restrict, suspend or ban instead")
    row = await _load_target(session, target_type, target_id, lock=True)
    author_id = getattr(row, AUTHOR_COLUMN[target_type])
    if target_type in STATUS_MODELS:
        if row.status == ContentStatus.removed:
            raise ConflictError("That is already removed")
        row.status = ContentStatus.removed
        row.hidden_at = row.hidden_at or datetime.now(UTC)
        await session.flush()
        if target_type == ReportTargetType.review:
            await ratings.recompute_for_review(session, target_id)
    elif target_type == ReportTargetType.message:
        removed = await messaging_service.remove_message_for_moderation(
            session, message_id=target_id
        )
        if not removed:
            raise ConflictError("That message is already deleted")
    elif not await groups_service.remove_group_for_moderation(session, group_id=target_id):
        raise ConflictError("That group is already deleted")

    resolved = await _resolve_pending(
        session,
        target_type=target_type,
        target_id=target_id,
        outcome=ReportStatus.upheld,
        staff=staff,
        note=reason,
    )
    await record_audit_event(
        session,
        action=AuditAction.content_removed,
        actor_type=AuditActorType.moderator,
        actor_id=staff.id,
        target_type=str(target_type),
        target_id=target_id,
        context={
            "author_id": str(author_id) if author_id else None,
            "reason": reason,
            "reports_upheld": resolved,
        },
    )
    await _tell(
        session,
        user_id=author_id,
        notice="content_removed",
        target_type=str(target_type),
        target_id=target_id,
        text=f"Your {_NOUN[target_type]} was taken down for breaking the community rules.",
        settings=settings,
    )
    return resolved


async def restore_content(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    note: str | None,
) -> int:
    """Put hidden, held or removed content back, dismissing the pending reports."""
    if target_type not in STATUS_MODELS:
        raise BadRequestError(
            "Only posts, comments, stories, listings, services, events and reviews can be restored"
        )
    row = await _load_target(session, target_type, target_id, lock=True)
    if row.deleted_at is not None:
        raise ConflictError("Its author deleted it; it cannot be restored")
    if row.status == ContentStatus.visible:
        raise ConflictError("That is already visible")
    row.status = ContentStatus.visible
    row.hidden_at = None
    await session.flush()
    if target_type == ReportTargetType.review:
        await ratings.recompute_for_review(session, target_id)
    resolved = await _resolve_pending(
        session,
        target_type=target_type,
        target_id=target_id,
        outcome=ReportStatus.dismissed,
        staff=staff,
        note=note,
    )
    await record_audit_event(
        session,
        action=AuditAction.content_restored,
        actor_type=AuditActorType.moderator,
        actor_id=staff.id,
        target_type=str(target_type),
        target_id=target_id,
        context={"reports_dismissed": resolved},
    )
    return resolved


async def resolve_reports(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    outcome: ReportStatus,
    note: str | None,
) -> int:
    """Close the pending reports about something without changing it: dismissed
    when nothing was wrong, upheld when the action was taken on the person."""
    if outcome == ReportStatus.pending:
        raise BadRequestError("Choose upheld or dismissed")
    resolved = await _resolve_pending(
        session,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        staff=staff,
        note=note,
    )
    if not resolved:
        raise ConflictError("Nothing is pending about that")
    await record_audit_event(
        session,
        action=AuditAction.report_resolved,
        actor_type=AuditActorType.moderator,
        actor_id=staff.id,
        target_type=str(target_type),
        target_id=target_id,
        context={"outcome": str(outcome), "reports": resolved},
    )
    return resolved


# ---------------------------------------------------------------------------
# Acting on accounts
# ---------------------------------------------------------------------------
async def _account(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id, with_for_update=True)
    if user is None or user.deleted_at is not None:
        raise NotFoundError("User not found")
    return user


async def _audit_user(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    action: AuditAction,
    user_id: uuid.UUID,
    context: dict[str, Any],
) -> None:
    await record_audit_event(
        session,
        action=action,
        actor_type=AuditActorType.moderator,
        actor_id=staff.id,
        target_type="user",
        target_id=user_id,
        context=context,
    )


async def warn(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    user_id: uuid.UUID,
    reason: str,
    report_id: uuid.UUID | None,
    settings: Settings,
) -> ModerationWarning:
    await _account(session, user_id)
    warning = ModerationWarning(
        user_id=user_id, staff_id=staff.id, reason=reason.strip(), report_id=report_id
    )
    session.add(warning)
    await session.flush()
    await _audit_user(
        session,
        staff=staff,
        action=AuditAction.user_warned,
        user_id=user_id,
        context={"warning_id": str(warning.id)},
    )
    await _tell(
        session,
        user_id=user_id,
        notice="warning",
        target_type="warning",
        target_id=warning.id,
        text="The moderators have sent you a warning. Open the app to read it.",
        settings=settings,
    )
    return warning


async def suspend(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    user_id: uuid.UUID,
    reason: str,
    days: int | None,
    settings: Settings,
) -> User:
    """Read-only for a while: they can still see why, and export their data."""
    user = await _account(session, user_id)
    if user.status in (UserStatus.banned, UserStatus.deletion_pending):
        raise ConflictError(f"The account is {user.status}", details={"status": str(user.status)})
    user.status = UserStatus.suspended
    user.suspended_until = datetime.now(UTC) + timedelta(days=days) if days else None
    await _audit_user(
        session,
        staff=staff,
        action=AuditAction.user_suspended,
        user_id=user_id,
        context={"reason": reason, "days": days},
    )
    ending = f" for {days} day{'s' if days != 1 else ''}" if days else ""
    await _tell(
        session,
        user_id=user_id,
        notice="suspended",
        target_type="user",
        target_id=user_id,
        text=f"Your account is suspended{ending}. You can still read, but not post.",
        settings=settings,
    )
    return user


async def unsuspend(session: AsyncSession, *, staff: StaffAccount, user_id: uuid.UUID) -> User:
    user = await _account(session, user_id)
    if user.status != UserStatus.suspended:
        raise ConflictError("The account is not suspended")
    user.status = UserStatus.active
    user.suspended_until = None
    await _audit_user(
        session, staff=staff, action=AuditAction.user_unsuspended, user_id=user_id, context={}
    )
    return user


async def restrict(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    user_id: uuid.UUID,
    reason: str,
    days: int,
    settings: Settings,
) -> User:
    user = await _account(session, user_id)
    user.restricted_until = datetime.now(UTC) + timedelta(days=days)
    user.restriction_reason = reason.strip()
    await _audit_user(
        session,
        staff=staff,
        action=AuditAction.user_restricted,
        user_id=user_id,
        context={"reason": reason, "days": days},
    )
    await _tell(
        session,
        user_id=user_id,
        notice="restricted",
        target_type="user",
        target_id=user_id,
        text="Your account is restricted for now: new posts wait for a moderator.",
        settings=settings,
    )
    return user


async def unrestrict(
    session: AsyncSession, *, staff: StaffAccount, user_id: uuid.UUID, note: str | None
) -> User:
    """Lift a restriction. Pending reports against the person are dismissed, so
    the same reports cannot restrict them again on the next one filed."""
    user = await _account(session, user_id)
    user.restricted_until = None
    user.restriction_reason = None
    dismissed = await _resolve_pending(
        session,
        target_type=ReportTargetType.user,
        target_id=user_id,
        outcome=ReportStatus.dismissed,
        staff=staff,
        note=note,
    )
    await _audit_user(
        session,
        staff=staff,
        action=AuditAction.user_unrestricted,
        user_id=user_id,
        context={"reports_dismissed": dismissed},
    )
    return user


_AUTHORED: tuple[tuple[Any, str], ...] = (
    (Post, "author_id"),
    (Comment, "author_id"),
    (Story, "author_id"),
    (Listing, "seller_id"),
    (ServicePost, "author_id"),
    (Event, "organiser_id"),
    (Review, "reviewer_id"),
)


async def ban(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    user_id: uuid.UUID,
    reason: str,
    ban_phone: bool,
    ban_devices: bool,
    remove_content: bool,
    settings: Settings,
) -> User:
    """Ban an account: signed out everywhere at once, and — unless told not to —
    its number and every device it signed in from are added to the ban lists, so
    a new account cannot simply be opened on the same phone."""
    user = await _account(session, user_id)
    if user.status == UserStatus.banned:
        raise ConflictError("The account is already banned")
    user.status = UserStatus.banned
    user.suspended_until = None
    revoked = await auth_service.revoke_sessions_for_moderation(session, user_id=user_id)

    phones = 0
    if ban_phone and user.phone_number:
        phones = await execute_dml(
            session,
            insert(BannedPhone)
            .values(
                phone_hash=banlist.phone_hash(user.phone_number, settings),
                reason=reason,
                staff_id=staff.id,
                user_id=user_id,
            )
            .on_conflict_do_nothing(index_elements=["phone_hash"]),
        )
    devices = 0
    if ban_devices:
        hashes = set(
            (
                await session.execute(
                    select(DeviceSession.device_id_hash)
                    .where(
                        DeviceSession.user_id == user_id,
                        DeviceSession.device_id_hash.is_not(None),
                    )
                    .distinct()
                )
            ).scalars()
        )
        for device_hash in sorted(h for h in hashes if h):
            devices += await execute_dml(
                session,
                insert(BannedDevice)
                .values(device_hash=device_hash, reason=reason, staff_id=staff.id, user_id=user_id)
                .on_conflict_do_nothing(index_elements=["device_hash"]),
            )
    removed = 0
    if remove_content:
        for model, column in _AUTHORED:
            removed += await execute_dml(
                session,
                update(model)
                .where(
                    getattr(model, column) == user_id,
                    model.status != ContentStatus.removed,
                    model.deleted_at.is_(None),
                )
                .values(status=ContentStatus.removed, hidden_at=datetime.now(UTC)),
            )
        # Their reviews no longer count towards anyone's rating.
        subjects = (
            await session.execute(
                select(Review.subject_id).where(Review.reviewer_id == user_id).distinct()
            )
        ).scalars()
        for subject_id in list(subjects):
            await ratings.recompute(session, subject_id)
    await _audit_user(
        session,
        staff=staff,
        action=AuditAction.user_banned,
        user_id=user_id,
        context={
            "reason": reason,
            "sessions_revoked": revoked,
            "phone_banned": bool(phones),
            "devices_banned": devices,
            "content_removed": removed,
        },
    )
    return user


async def unban(session: AsyncSession, *, staff: StaffAccount, user_id: uuid.UUID) -> User:
    """Lift a ban, and the phone and device bans that came with it."""
    user = await _account(session, user_id)
    if user.status != UserStatus.banned:
        raise ConflictError("The account is not banned")
    user.status = UserStatus.active
    phones = await execute_dml(session, delete(BannedPhone).where(BannedPhone.user_id == user_id))
    devices = await execute_dml(
        session, delete(BannedDevice).where(BannedDevice.user_id == user_id)
    )
    await _audit_user(
        session,
        staff=staff,
        action=AuditAction.user_unbanned,
        user_id=user_id,
        context={"phone_bans_lifted": phones, "device_bans_lifted": devices},
    )
    return user


async def revoke_badge(
    session: AsyncSession, *, staff: StaffAccount, user_id: uuid.UUID, lock: bool
) -> User:
    """Take the verified-neighbour badge away. Locked, the daily recomputation
    leaves it off until a moderator unlocks it."""
    user = await _account(session, user_id)
    user.is_verified_neighbour = False
    user.verified_neighbour_at = None
    if lock:
        user.badge_locked_at = datetime.now(UTC)
    await _audit_user(
        session,
        staff=staff,
        action=AuditAction.badge_revoked,
        user_id=user_id,
        context={"locked": lock},
    )
    return user


async def unlock_badge(session: AsyncSession, *, staff: StaffAccount, user_id: uuid.UUID) -> User:
    """Let the daily recomputation award the badge again, if the rule holds."""
    user = await _account(session, user_id)
    if user.badge_locked_at is None:
        raise ConflictError("The badge is not locked")
    user.badge_locked_at = None
    await _audit_user(
        session,
        staff=staff,
        action=AuditAction.badge_revoked,
        user_id=user_id,
        context={"unlocked": True},
    )
    return user


# ---------------------------------------------------------------------------
# A person's history
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class UserHistory:
    user: User
    reports_against: list[Report]
    reports_by: list[Report]
    warnings: list[ModerationWarning]
    actions: list[AuditEvent]
    upheld_against_content: int


HISTORY_LIMIT = 50


async def history(session: AsyncSession, user_id: uuid.UUID) -> UserHistory:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    against = (
        await session.execute(
            select(Report)
            .where(Report.target_type == ReportTargetType.user, Report.target_id == user_id)
            .order_by(Report.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars()
    by = (
        await session.execute(
            select(Report)
            .where(Report.reporter_id == user_id)
            .order_by(Report.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars()
    warnings = (
        await session.execute(
            select(ModerationWarning)
            .where(ModerationWarning.user_id == user_id)
            .order_by(ModerationWarning.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars()
    actions = (
        await session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.actor_type.in_([AuditActorType.moderator, AuditActorType.system]),
                or_(
                    and_(AuditEvent.target_type == "user", AuditEvent.target_id == user_id),
                    AuditEvent.context["author_id"].astext == str(user_id),
                ),
            )
            .order_by(AuditEvent.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars()
    upheld = await session.scalar(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action == AuditAction.content_removed,
            AuditEvent.context["author_id"].astext == str(user_id),
        )
    )
    return UserHistory(
        user=user,
        reports_against=list(against),
        reports_by=list(by),
        warnings=list(warnings),
        actions=list(actions),
        upheld_against_content=int(upheld or 0),
    )


# ---------------------------------------------------------------------------
# Ban lists
# ---------------------------------------------------------------------------
async def ban_phone_number(
    session: AsyncSession,
    *,
    staff: StaffAccount,
    phone_number: str,
    reason: str | None,
    settings: Settings,
) -> BannedPhone:
    digest = banlist.phone_hash(phone_number, settings)
    existing = await session.scalar(select(BannedPhone).where(BannedPhone.phone_hash == digest))
    if existing is not None:
        raise ConflictError("That number is already banned", details={"ban_id": str(existing.id)})
    owner = await session.scalar(select(User.id).where(User.phone_number == phone_number))
    ban_row = BannedPhone(phone_hash=digest, reason=reason, staff_id=staff.id, user_id=owner)
    session.add(ban_row)
    await session.flush()
    await record_audit_event(
        session,
        action=AuditAction.phone_banned,
        actor_type=AuditActorType.moderator,
        actor_id=staff.id,
        target_type="banned_phone",
        target_id=ban_row.id,
        context={"has_account": owner is not None},
    )
    return ban_row


async def lift_ban_entry(
    session: AsyncSession, *, staff: StaffAccount, model: Any, ban_id: uuid.UUID
) -> None:
    row = await session.get(model, ban_id)
    if row is None:
        raise NotFoundError("No such ban")
    await session.delete(row)
    await record_audit_event(
        session,
        action=AuditAction.phone_unbanned if model is BannedPhone else AuditAction.device_unbanned,
        actor_type=AuditActorType.moderator,
        actor_id=staff.id,
        target_type="banned_phone" if model is BannedPhone else "banned_device",
        target_id=ban_id,
    )


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------
async def lift_expired_suspensions(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Hourly: reactivate accounts whose suspension has run its course. Access
    checks already treat a lapsed suspension as lifted; this makes the status say so."""
    now = now or datetime.now(UTC)
    lifted = list(
        (
            await session.execute(
                update(User)
                .where(
                    User.status == UserStatus.suspended,
                    User.suspended_until.is_not(None),
                    User.suspended_until <= now,
                )
                .values(status=UserStatus.active, suspended_until=None)
                .returning(User.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars()
    )
    for user_id in lifted:
        await record_audit_event(
            session,
            action=AuditAction.user_unsuspended,
            actor_type=AuditActorType.system,
            target_type="user",
            target_id=user_id,
            context={"expired": True},
        )
    return len(lifted)
