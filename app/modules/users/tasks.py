"""Retention work for accounts.

The Data Protection Act's erasure right, implemented as a grace period followed
by an irreversible purge. Both halves matter: the purge because the right is real,
and the grace period because account deletion is frequently regretted and
sometimes coerced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.modules.auth import service as auth_service
from app.modules.auth.models import DeviceSession
from app.modules.users.models import User
from app.modules.users.service import purge_user

logger = logging.getLogger(__name__)

# A cap per run, so one scheduled job cannot hold a long transaction open over
# thousands of rows. Whatever is left is picked up on the next run.
PURGE_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class PurgeResult:
    accounts_purged: int
    cutoff: datetime


async def purge_deleted_accounts(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> PurgeResult:
    """Purge accounts whose grace period has run out."""
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=settings.deletion_grace_period_days)

    statement = (
        select(User)
        .where(
            User.deletion_requested_at.is_not(None),
            User.deletion_requested_at <= cutoff,
            User.deleted_at.is_(None),
        )
        .order_by(User.deletion_requested_at.asc())
        .limit(PURGE_BATCH_SIZE)
    )
    due = list((await session.execute(statement)).scalars().all())

    for user in due:
        await auth_service.revoke_sessions_for_deleted_account(session, user_id=user.id)
        await purge_user(session, user=user)

    if due:
        logger.info(
            "accounts_purged",
            extra={"count": len(due), "grace_period_days": settings.deletion_grace_period_days},
        )
    return PurgeResult(accounts_purged=len(due), cutoff=cutoff)


async def delete_expired_sessions(
    session: AsyncSession, *, now: datetime | None = None, retain_days: int = 30
) -> int:
    """Remove device sessions that expired a while ago.

    Not security-critical — an expired session is already refused on its own
    expiry — but the rows accumulate, and the revoked ones are kept for a window
    first so a reuse-detection event can still be investigated afterwards.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=retain_days)
    removed = await execute_dml(
        session, delete(DeviceSession).where(DeviceSession.expires_at < cutoff)
    )
    if removed:
        logger.info("expired_sessions_deleted", extra={"count": removed})
    return removed
