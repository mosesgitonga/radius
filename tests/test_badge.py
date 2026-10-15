"""The verified-neighbour badge: the earned route, the vouched route, locks, and
withdrawal when the rule stops holding."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.moderation.models import (
    AuditAction,
    AuditEvent,
    Report,
    ReportReason,
    ReportStatus,
    ReportTargetType,
)
from app.modules.users import badge
from app.modules.users.models import User, Vouch
from app.workers.tasks import recompute_badges as recompute_badges_task
from tests.factories import NYERI_TOWN, Coordinates, make_user, offset_km

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]

TEN_DAYS = 0b00001111111111
NINE_DAYS = 0b00000111111111


async def resident(
    db_session: AsyncSession,
    *,
    mask: int = TEN_DAYS,
    age_days: float = 20,
    at: Coordinates = NYERI_TOWN,
    is_verified_neighbour: bool = False,
) -> User:
    """Someone at home on as many of the last fourteen days as `mask` says."""
    user = await make_user(
        db_session, at=at, created_days_ago=age_days, is_verified_neighbour=is_verified_neighbour
    )
    user.home_presence_mask = mask
    user.home_presence_updated_on = datetime.now(UTC).date()
    await db_session.flush()
    return user


async def run(db_session: AsyncSession, settings: Settings) -> badge.BadgeRun:
    result = await badge.recompute_badges(db_session, settings=settings)
    await db_session.flush()
    return result


async def refreshed(db_session: AsyncSession, user: User) -> User:
    await db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Earned
# ---------------------------------------------------------------------------
async def test_an_established_resident_earns_the_badge(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await resident(db_session)
    result = await run(db_session, settings)
    assert result.awarded == 1
    assert (await refreshed(db_session, user)).is_verified_neighbour
    assert user.verified_neighbour_at is not None
    audit = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.badge_awarded)
        )
    ).scalar_one()
    assert audit.target_id == user.id
    assert audit.actor_type == "system"


async def test_each_condition_is_needed(db_session: AsyncSession, settings: Settings) -> None:
    too_new = await resident(db_session, age_days=settings.badge_min_account_age_days - 1)
    too_absent = await resident(db_session, mask=NINE_DAYS)
    restricted = await resident(db_session)
    restricted.restricted_until = datetime.now(UTC) + timedelta(days=3)
    unverified = await resident(db_session)
    unverified.phone_verified_at = None
    reported = await resident(db_session)
    reporter = await make_user(db_session, at=NYERI_TOWN)
    db_session.add(
        Report(
            reporter_id=reporter.id,
            target_type=ReportTargetType.user,
            target_id=reported.id,
            reason=ReportReason.harassment,
            status=ReportStatus.upheld,
            resolved_at=datetime.now(UTC) - timedelta(days=10),
        )
    )
    await db_session.flush()

    result = await run(db_session, settings)
    assert result.awarded == 0
    for user in (too_new, too_absent, restricted, unverified, reported):
        assert not (await refreshed(db_session, user)).is_verified_neighbour


async def test_an_old_upheld_report_no_longer_counts(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await resident(db_session)
    reporter = await make_user(db_session, at=NYERI_TOWN)
    db_session.add(
        Report(
            reporter_id=reporter.id,
            target_type=ReportTargetType.user,
            target_id=user.id,
            reason=ReportReason.spam,
            status=ReportStatus.upheld,
            resolved_at=datetime.now(UTC) - timedelta(days=settings.badge_report_lookback_days + 1),
        )
    )
    await db_session.flush()
    assert (await run(db_session, settings)).awarded == 1


async def test_the_badge_is_withdrawn_when_the_rule_stops_holding(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await resident(db_session)
    await run(db_session, settings)
    assert (await refreshed(db_session, user)).is_verified_neighbour

    user.home_presence_mask = NINE_DAYS
    await db_session.flush()
    result = await run(db_session, settings)
    assert result.revoked == 1
    assert not (await refreshed(db_session, user)).is_verified_neighbour
    assert user.verified_neighbour_at is None


async def test_a_lock_keeps_it_off(db_session: AsyncSession, settings: Settings) -> None:
    user = await resident(db_session, is_verified_neighbour=True)
    user.badge_locked_at = datetime.now(UTC)
    await db_session.flush()
    result = await run(db_session, settings)
    assert result.revoked == 1
    assert result.awarded == 0
    assert not (await refreshed(db_session, user)).is_verified_neighbour

    user.badge_locked_at = None
    await db_session.flush()
    assert (await run(db_session, settings)).awarded == 1


async def test_the_nightly_task_rolls_presence_first(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A mask last updated twelve days ago has only two days left in the window."""
    user = await resident(db_session)
    user.home_presence_updated_on = datetime.now(UTC).date() - timedelta(days=12)
    await db_session.flush()
    result = await recompute_badges_task({"session": db_session})
    assert result == {"awarded": 0, "revoked": 0}
    assert not (await refreshed(db_session, user)).is_verified_neighbour


# ---------------------------------------------------------------------------
# Vouched
# ---------------------------------------------------------------------------
async def test_two_verified_neighbours_vouching_earn_it(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    newcomer = await resident(db_session, mask=0, age_days=1)
    first = await resident(db_session, is_verified_neighbour=True)
    second = await resident(db_session, is_verified_neighbour=True)
    newcomer_id = newcomer.id
    for voucher in (first, second):
        response = await (await authenticate(voucher)).put(f"/v1/users/{newcomer_id}/vouch")
        assert response.status_code == 200, response.text
        assert response.json() == {"vouchee_id": str(newcomer_id), "vouched": True}

    await run(db_session, settings)
    assert (await refreshed(db_session, newcomer)).is_verified_neighbour
    standing = (await (await authenticate(newcomer)).get("/v1/me/standing")).json()
    assert standing["vouches_received"] == 2


async def test_vouches_from_another_area_do_not_count(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    newcomer = await resident(db_session, mask=0, age_days=1)
    near = await resident(db_session, is_verified_neighbour=True)
    far = await resident(
        db_session, is_verified_neighbour=True, at=offset_km(NYERI_TOWN, north=30.0)
    )
    newcomer_id = newcomer.id
    for voucher in (near, far):
        response = await (await authenticate(voucher)).put(f"/v1/users/{newcomer_id}/vouch")
        # Accepted either way: refusing would reveal where the newcomer lives.
        assert response.status_code == 200

    await run(db_session, settings)
    assert not (await refreshed(db_session, newcomer)).is_verified_neighbour


async def test_only_verified_neighbours_vouch(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    unverified = await resident(db_session)
    other = await resident(db_session)
    response = await (await authenticate(unverified)).put(f"/v1/users/{other.id}/vouch")
    assert response.status_code == 403
    verified = await resident(db_session, is_verified_neighbour=True)
    me = await authenticate(verified)
    assert (await me.put(f"/v1/users/{verified.id}/vouch")).status_code == 400


async def test_vouching_is_idempotent_and_withdrawable(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    voucher = await resident(db_session, is_verified_neighbour=True)
    other = await resident(db_session)
    other_id = other.id
    me = await authenticate(voucher)
    for _ in range(2):
        assert (await me.put(f"/v1/users/{other_id}/vouch")).status_code == 200
    count = await db_session.scalar(select(func.count()).select_from(Vouch))
    assert count == 1
    withdrawn = await (await authenticate(voucher)).delete(f"/v1/users/{other_id}/vouch")
    assert withdrawn.json()["vouched"] is False
    assert await db_session.scalar(select(func.count()).select_from(Vouch)) == 0
