"""The verified-neighbour badge: vouches, and the daily recomputation.

The badge says "this person really lives around here". It is earned one of two
ways, and recomputed every night, so it is withdrawn as soon as the rule stops
holding:

* **Earned** — a verified phone, an account at least `BADGE_MIN_ACCOUNT_AGE_DAYS`
  old, seen in their home area on at least `BADGE_MIN_PRESENCE_DAYS` of the last
  fourteen days (the presence mask — one bit a day, the only location history kept),
  and nothing upheld against them or their content in `BADGE_REPORT_LOOKBACK_DAYS`.
* **Vouched** — `BADGE_VOUCHES_REQUIRED` verified neighbours who live in the same
  res-7 area have vouched for them. The area is checked when the badge is
  computed, not when the vouch was given, so a voucher who moves away stops
  counting.

Either way the account must be active and not restricted, and a moderator's lock
keeps the badge off whatever the rule says.

A vouch can be given to anyone the voucher can see. Whether they share an area is
deliberately not checked at the time: refusing a vouch "because you are not
neighbours" would tell the voucher where someone lives, to within a res-7 cell.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, String, and_, cast, delete, exists, func, or_, select, union, update
from sqlalchemy.dialects.postgresql import BIT, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, ForbiddenError, NotFoundError, RateLimitedError
from app.core.ratelimit import get_rate_limiter
from app.modules.moderation.models import (
    AuditAction,
    AuditActorType,
    AuditEvent,
    Report,
    ReportStatus,
    ReportTargetType,
)
from app.modules.users.models import HOME_PRESENCE_WINDOW_DAYS, User, UserStatus, Vouch
from app.modules.users.service import is_blocked_between

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vouches
# ---------------------------------------------------------------------------
async def vouch(
    session: AsyncSession, *, voucher: User, vouchee_id: uuid.UUID, settings: Settings
) -> bool:
    """Vouch for a neighbour. Returns whether a new vouch was recorded."""
    if vouchee_id == voucher.id:
        raise BadRequestError("You cannot vouch for yourself")
    if not voucher.is_verified_neighbour:
        raise ForbiddenError(
            "Only verified neighbours can vouch for others",
            details={"reason": "not_verified_neighbour"},
        )
    limited = await get_rate_limiter().hit(
        "vouch", f"u:{voucher.id}", limit=settings.vouches_per_day, window_seconds=86_400
    )
    if not limited.allowed:
        raise RateLimitedError(retry_after_seconds=limited.retry_after_seconds)
    vouchee = await session.get(User, vouchee_id)
    if (
        vouchee is None
        or vouchee.deleted_at is not None
        or vouchee.status == UserStatus.banned
        or await is_blocked_between(session, voucher.id, vouchee_id)
    ):
        raise NotFoundError("User not found")
    created = await execute_dml(
        session,
        insert(Vouch)
        .values(voucher_id=voucher.id, vouchee_id=vouchee_id)
        .on_conflict_do_nothing(constraint="uq_vouches_voucher_id_vouchee_id"),
    )
    return bool(created)


async def withdraw_vouch(session: AsyncSession, *, voucher: User, vouchee_id: uuid.UUID) -> None:
    await execute_dml(
        session,
        delete(Vouch).where(Vouch.voucher_id == voucher.id, Vouch.vouchee_id == vouchee_id),
    )


async def has_vouched(
    session: AsyncSession, *, voucher_id: uuid.UUID, vouchee_id: uuid.UUID
) -> bool:
    found = await session.scalar(
        select(Vouch.id).where(Vouch.voucher_id == voucher_id, Vouch.vouchee_id == vouchee_id)
    )
    return found is not None


async def vouches_given(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(Vouch.vouchee_id)
                .where(Vouch.voucher_id == user_id)
                .order_by(Vouch.created_at.desc())
            )
        ).scalars()
    )


async def vouches_received_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(func.count(Vouch.id)).where(Vouch.vouchee_id == user_id)) or 0
    )


# ---------------------------------------------------------------------------
# The daily recomputation
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BadgeRun:
    awarded: int
    revoked: int


AUDIT_BATCH = 1000


def qualifying_query(settings: Settings, now: datetime) -> Select[tuple[uuid.UUID]]:
    """Everyone the rule awards the badge to today, locks aside, as one query —
    set-based, because on the first night that is most of a town."""
    lookback = now - timedelta(days=settings.badge_report_lookback_days)
    standing = and_(
        User.status == UserStatus.active,
        User.deleted_at.is_(None),
        User.phone_verified_at.is_not(None),
        User.home_cell_res7.is_not(None),
        or_(User.restricted_until.is_(None), User.restricted_until <= now),
    )
    upheld_against_person = exists().where(
        Report.target_type == ReportTargetType.user,
        Report.target_id == User.id,
        Report.status == ReportStatus.upheld,
        Report.resolved_at >= lookback,
    )
    content_removed = exists().where(
        AuditEvent.action == AuditAction.content_removed,
        AuditEvent.context["author_id"].astext == cast(User.id, String),
        AuditEvent.created_at >= lookback,
    )
    # Days present: the set bits of the 14-bit mask.
    presence_days = func.length(
        func.replace(
            cast(cast(User.home_presence_mask, BIT(HOME_PRESENCE_WINDOW_DAYS)), String), "0", ""
        )
    )
    earned = select(User.id).where(
        standing,
        User.created_at <= now - timedelta(days=settings.badge_min_account_age_days),
        presence_days >= settings.badge_min_presence_days,
        ~upheld_against_person,
        ~content_removed,
    )
    voucher = aliased(User)
    vouched = (
        select(Vouch.vouchee_id)
        .join(User, User.id == Vouch.vouchee_id)
        .join(voucher, voucher.id == Vouch.voucher_id)
        .where(
            standing,
            ~upheld_against_person,
            voucher.is_verified_neighbour.is_(True),
            voucher.status == UserStatus.active,
            voucher.deleted_at.is_(None),
            voucher.home_cell_res7 == User.home_cell_res7,
        )
        .group_by(Vouch.vouchee_id)
        .having(func.count(Vouch.id) >= settings.badge_vouches_required)
    )
    return union(earned, vouched)  # type: ignore[return-value]


async def recompute_badges(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> BadgeRun:
    """Withdraw the badge from everyone who no longer qualifies or is locked, then
    award it to everyone who qualifies and has none."""
    now = now or datetime.now(UTC)
    revoked = list(
        (
            await session.execute(
                update(User)
                .where(
                    User.is_verified_neighbour.is_(True),
                    or_(
                        User.badge_locked_at.is_not(None),
                        User.id.not_in(qualifying_query(settings, now).scalar_subquery()),
                    ),
                )
                .values(is_verified_neighbour=False, verified_neighbour_at=None)
                .returning(User.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars()
    )
    awarded = list(
        (
            await session.execute(
                update(User)
                .where(
                    User.is_verified_neighbour.is_(False),
                    User.badge_locked_at.is_(None),
                    User.id.in_(qualifying_query(settings, now).scalar_subquery()),
                )
                .values(is_verified_neighbour=True, verified_neighbour_at=now)
                .returning(User.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars()
    )
    await _audit_all(session, AuditAction.badge_revoked, revoked, now)
    await _audit_all(session, AuditAction.badge_awarded, awarded, now)
    logger.info("badges_recomputed", extra={"awarded": len(awarded), "revoked": len(revoked)})
    return BadgeRun(awarded=len(awarded), revoked=len(revoked))


async def _audit_all(
    session: AsyncSession, action: AuditAction, user_ids: list[uuid.UUID], now: datetime
) -> None:
    """One system audit row per change, inserted in batches."""
    for start in range(0, len(user_ids), AUDIT_BATCH):
        await session.execute(
            insert(AuditEvent),
            [
                {
                    "actor_type": AuditActorType.system,
                    "action": action,
                    "target_type": "user",
                    "target_id": user_id,
                    "context": {},
                    "created_at": now,
                }
                for user_id in user_ids[start : start + AUDIT_BATCH]
            ],
        )
