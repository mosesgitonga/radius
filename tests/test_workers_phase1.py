"""Phase 1 background work.

Tasks are called directly with a stub context, as the brief specifies. Each one
here is checked against real rows rather than mocks, because the bugs these jobs
can have — a cell whose count never clears, a presence mask shifted by the wrong
number of days — are invisible until the data is wrong.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.auth import service as auth_service
from app.modules.auth.models import DeviceSession, SessionRevocationReason
from app.modules.location import cells
from app.modules.location import service as location_service
from app.modules.location.models import CellActivity
from app.modules.location.tasks import refresh_cell_activity, roll_home_presence
from app.modules.users.models import HOME_PRESENCE_WINDOW_DAYS, User, UserStatus
from app.modules.users.tasks import (
    delete_expired_sessions,
    purge_deleted_accounts,
)
from app.workers.settings import WorkerSettings
from app.workers.tasks import heartbeat
from tests.factories import (
    DEKUT_CAMPUS,
    KARATINA,
    NYERI_TOWN,
    make_user,
    make_users_at,
    offset_km,
)


# ---------------------------------------------------------------------------
# Density refresh
# ---------------------------------------------------------------------------
async def test_the_refresh_counts_users_at_every_resolution(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Resolution 8 feeds the radius engine; 7 and 6 feed k-anonymity. A refresh
    that missed one would silently break whichever feature depended on it."""
    await make_users_at(db_session, origin=NYERI_TOWN, count=12, radius_km=0.5, seed=1)

    await refresh_cell_activity(db_session, settings=settings)

    resolutions = {
        row[0] for row in (await db_session.execute(select(CellActivity.resolution))).all()
    }
    assert resolutions == {8, 7, 6}


async def test_the_refresh_separates_active_from_registered(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Density uses the active count; k-anonymity uses the registered one. They
    are different questions and must not be the same number."""
    await make_users_at(db_session, origin=NYERI_TOWN, count=5, radius_km=0.2, seed=2)
    await make_users_at(db_session, origin=NYERI_TOWN, count=7, radius_km=0.2, seed=3, active=False)

    await refresh_cell_activity(db_session, settings=settings)

    rows = (
        (await db_session.execute(select(CellActivity).where(CellActivity.resolution == 6)))
        .scalars()
        .all()
    )
    totals_active = sum(row.active_user_count for row in rows)
    totals_registered = sum(row.registered_user_count for row in rows)

    assert totals_active == 5
    assert totals_registered == 12


async def test_the_refresh_ignores_users_with_no_home(
    db_session: AsyncSession, settings: Settings
) -> None:
    await make_user(db_session)
    await make_user(db_session)

    result = await refresh_cell_activity(db_session, settings=settings)

    assert result.cells_updated == 0


async def test_the_refresh_ignores_deleted_users(
    db_session: AsyncSession, settings: Settings
) -> None:
    gone = await make_user(db_session, at=NYERI_TOWN)
    gone.deleted_at = datetime.now(UTC)
    await db_session.flush()

    await refresh_cell_activity(db_session, settings=settings)

    rows = (await db_session.execute(select(CellActivity))).scalars().all()
    assert all(row.registered_user_count == 0 for row in rows) or not rows


async def test_the_refresh_counts_only_active_accounts_as_active(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A banned account is still a resident for k-anonymity, but is not active
    traffic the radius should shrink around."""
    await make_user(db_session, at=NYERI_TOWN, status=UserStatus.banned)

    await refresh_cell_activity(db_session, settings=settings)

    rows = (
        (await db_session.execute(select(CellActivity).where(CellActivity.resolution == 8)))
        .scalars()
        .all()
    )
    assert rows
    assert rows[0].active_user_count == 0
    assert rows[0].registered_user_count == 1


async def test_rerunning_the_refresh_does_not_double_count(
    db_session: AsyncSession, settings: Settings
) -> None:
    """It runs every fifteen minutes for ever, so an upsert that accumulated
    would make every radius wrong within a day."""
    await make_users_at(db_session, origin=NYERI_TOWN, count=8, radius_km=0.2, seed=4)

    await refresh_cell_activity(db_session, settings=settings)
    first = (
        (
            await db_session.execute(
                select(CellActivity.registered_user_count).where(CellActivity.resolution == 6)
            )
        )
        .scalars()
        .all()
    )
    await refresh_cell_activity(db_session, settings=settings)
    second = (
        (
            await db_session.execute(
                select(CellActivity.registered_user_count).where(CellActivity.resolution == 6)
            )
        )
        .scalars()
        .all()
    )

    assert sorted(first) == sorted(second)


async def test_an_emptied_cell_is_removed_not_left_stale(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Rows this run did not touch are deleted. Without that, a cell whose last
    resident moves away keeps its count for ever and the radius around it stays
    wrong indefinitely."""
    movers = await make_users_at(db_session, origin=KARATINA, count=6, radius_km=0.2, seed=6)
    await refresh_cell_activity(db_session, settings=settings)
    assert (await db_session.execute(select(CellActivity))).scalars().all()

    for mover in movers:
        mover.home_cell_res9 = None
        mover.home_cell_res8 = None
        mover.home_cell_res7 = None
        mover.home_cell_res6 = None
        mover.home_centroid = None
    await db_session.flush()

    result = await refresh_cell_activity(db_session, settings=settings)

    assert result.stale_cells_removed > 0
    assert (await db_session.execute(select(CellActivity))).scalars().all() == []


async def test_the_refresh_records_when_it_ran(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Staleness has to be visible, so "nobody is here" can be told apart from
    "the refresh has not run"."""
    await make_users_at(db_session, origin=NYERI_TOWN, count=3, radius_km=0.2, seed=7)
    before = datetime.now(UTC)

    await refresh_cell_activity(db_session, settings=settings)

    rows = (await db_session.execute(select(CellActivity))).scalars().all()
    assert all(row.computed_at >= before - timedelta(seconds=5) for row in rows)


async def test_users_in_different_cells_are_counted_separately(
    db_session: AsyncSession, settings: Settings
) -> None:
    await make_users_at(db_session, origin=NYERI_TOWN, count=4, radius_km=0.1, seed=8)
    await make_users_at(db_session, origin=KARATINA, count=9, radius_km=0.1, seed=9)

    await refresh_cell_activity(db_session, settings=settings)

    nyeri_cell = cells.locate(*NYERI_TOWN.as_tuple()).cell_res6
    karatina_cell = cells.locate(*KARATINA.as_tuple()).cell_res6
    counts = {
        row.cell: row.registered_user_count
        for row in (
            await db_session.execute(select(CellActivity).where(CellActivity.resolution == 6))
        ).scalars()
    }

    assert counts[nyeri_cell] == 4
    assert counts[karatina_cell] == 9


# ---------------------------------------------------------------------------
# Home presence
# ---------------------------------------------------------------------------
def test_a_fresh_mask_starts_empty() -> None:
    assert location_service.roll_presence_mask(0b1111, None, date(2026, 3, 1), window_days=14) == 0


def test_rolling_on_the_same_day_changes_nothing() -> None:
    today = date(2026, 3, 1)

    assert location_service.roll_presence_mask(0b1011, today, today, window_days=14) == 0b1011


def test_rolling_one_day_shifts_by_one() -> None:
    """Bit 0 is today, so advancing a day moves everything up one place."""
    rolled = location_service.roll_presence_mask(
        0b0000_0000_0001, date(2026, 3, 1), date(2026, 3, 2), window_days=14
    )

    assert rolled == 0b0000_0000_0010


def test_rolling_several_days_shifts_by_that_many() -> None:
    """The reason the last-rolled date is stored at all. Shifting by one per
    update would record somebody absent for a week as present throughout, and
    the badge would reward absence."""
    rolled = location_service.roll_presence_mask(
        0b111, date(2026, 3, 1), date(2026, 3, 6), window_days=14
    )

    assert rolled == 0b111_00000


def test_an_absence_longer_than_the_window_clears_everything() -> None:
    rolled = location_service.roll_presence_mask(
        0b1111_1111_1111, date(2026, 3, 1), date(2026, 4, 1), window_days=14
    )

    assert rolled == 0


def test_the_mask_never_grows_past_its_window() -> None:
    """The column has a CHECK constraint on this, so overflowing would not be a
    wrong answer but a failed write."""
    full = (1 << HOME_PRESENCE_WINDOW_DAYS) - 1

    rolled = location_service.roll_presence_mask(
        full, date(2026, 3, 1), date(2026, 3, 8), window_days=HOME_PRESENCE_WINDOW_DAYS
    )

    assert rolled < (1 << HOME_PRESENCE_WINDOW_DAYS)


async def test_reporting_a_location_at_home_sets_today(
    db_session: AsyncSession, settings: Settings, user: User
) -> None:
    location_service.record_home_presence(
        user,
        reported_cell_res9=user.home_cell_res9,
        today=date(2026, 3, 1),
        settings=settings,
    )

    assert user.home_presence_mask & 1
    assert user.home_presence_updated_on == date(2026, 3, 1)


async def test_reporting_a_location_elsewhere_does_not_set_today(
    db_session: AsyncSession, settings: Settings, user: User
) -> None:
    far = cells.locate(*KARATINA.as_tuple()).cell_res9

    location_service.record_home_presence(
        user, reported_cell_res9=far, today=date(2026, 3, 1), settings=settings
    )

    assert not user.home_presence_mask & 1


async def test_presence_is_judged_by_neighbourhood_not_exact_cell(
    db_session: AsyncSession, settings: Settings, user: User
) -> None:
    """ "At home" means the neighbourhood: someone at the shops two streets away
    is still at home in every sense that matters."""
    nearby = cells.locate(*offset_km(NYERI_TOWN, north=0.3).as_tuple()).cell_res9
    assert nearby != user.home_cell_res9

    location_service.record_home_presence(
        user, reported_cell_res9=nearby, today=date(2026, 3, 1), settings=settings
    )

    assert user.home_presence_mask & 1


async def test_an_excluded_day_is_neither_yes_nor_no(
    db_session: AsyncSession, settings: Settings, user: User
) -> None:
    """Explore mode. Penalising a deliberate temporary mode would make the badge
    measure "did not travel" rather than "lives here"."""
    location_service.record_home_presence(
        user, reported_cell_res9=None, today=date(2026, 3, 1), settings=settings
    )

    assert user.home_presence_mask == 0
    assert user.home_presence_updated_on == date(2026, 3, 1)


async def test_the_roll_task_ages_stale_masks(db_session: AsyncSession, user: User) -> None:
    user.home_presence_mask = 0b11
    user.home_presence_updated_on = date(2026, 3, 1)
    await db_session.flush()

    rolled = await roll_home_presence(db_session, today=date(2026, 3, 4))

    assert rolled == 1
    await db_session.refresh(user)
    assert user.home_presence_mask == 0b11000
    assert user.home_presence_updated_on == date(2026, 3, 4)


async def test_the_roll_task_skips_users_already_rolled_today(
    db_session: AsyncSession, user: User
) -> None:
    user.home_presence_mask = 0b11
    user.home_presence_updated_on = date(2026, 3, 4)
    await db_session.flush()

    rolled = await roll_home_presence(db_session, today=date(2026, 3, 4))

    assert rolled == 0
    await db_session.refresh(user)
    assert user.home_presence_mask == 0b11


async def test_the_roll_task_clears_a_long_absence(db_session: AsyncSession, user: User) -> None:
    user.home_presence_mask = 0b1111
    user.home_presence_updated_on = date(2026, 3, 1)
    await db_session.flush()

    await roll_home_presence(db_session, today=date(2026, 5, 1))

    await db_session.refresh(user)
    assert user.home_presence_mask == 0


async def test_changing_home_resets_the_presence_window(
    db_session: AsyncSession, redis_client: object, settings: Settings, user: User
) -> None:
    """Recommendation accepted with "continue": the window measures consistency
    with a home area that has just changed, so it starts again."""
    user.home_presence_mask = 0b1111_1111
    user.home_presence_updated_on = datetime.now(UTC).date()
    user.home_changed_at = None
    await db_session.flush()

    await location_service.set_home_location(
        db_session,
        redis_client,  # type: ignore[arg-type]
        user=user,
        latitude=DEKUT_CAMPUS.latitude,
        longitude=DEKUT_CAMPUS.longitude,
        settings=settings,
    )

    # Reset, then today's own report sets bit 0 again.
    assert user.home_presence_mask == 1


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
async def test_an_account_past_its_grace_period_is_purged(
    db_session: AsyncSession, settings: Settings
) -> None:
    doomed = await make_user(db_session, at=NYERI_TOWN)
    doomed.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()

    result = await purge_deleted_accounts(db_session, settings=settings)

    assert result.accounts_purged == 1
    await db_session.refresh(doomed)
    assert doomed.deleted_at is not None
    assert doomed.display_name == "Deleted user"
    assert doomed.home_cell_res9 is None
    assert doomed.home_centroid is None


async def test_an_account_inside_its_grace_period_is_left_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Reversible for the whole window, because deletion is frequently regretted
    and sometimes coerced."""
    recent = await make_user(db_session, at=NYERI_TOWN)
    recent.deletion_requested_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()

    result = await purge_deleted_accounts(db_session, settings=settings)

    assert result.accounts_purged == 0
    await db_session.refresh(recent)
    assert recent.deleted_at is None


async def test_purging_clears_the_phone_number_entirely(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The row stays, because other tables reference it and a cascade would take
    moderation evidence with it — but the number is gone, not replaced with a
    placeholder. Null is the honest record of an erasure."""
    doomed = await make_user(db_session, at=NYERI_TOWN)
    original = doomed.phone_number
    doomed.deletion_requested_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.flush()

    await purge_deleted_accounts(db_session, settings=settings)

    await db_session.refresh(doomed)
    assert doomed.phone_number is None
    assert original is not None


async def test_several_purged_accounts_can_coexist(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The unique index on phone_number permits many nulls, which is why no
    placeholder needs inventing to keep purged rows distinct."""
    for _ in range(3):
        doomed = await make_user(db_session, at=NYERI_TOWN)
        doomed.deletion_requested_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.flush()

    result = await purge_deleted_accounts(db_session, settings=settings)

    assert result.accounts_purged == 3


async def test_a_purged_number_cannot_sign_in_again(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The point of clearing it: the number must not reach the purged account."""
    from app.modules.users.service import get_user_by_phone

    doomed = await make_user(db_session, at=NYERI_TOWN)
    original = doomed.phone_number
    assert original is not None
    doomed.deletion_requested_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.flush()

    await purge_deleted_accounts(db_session, settings=settings)
    await db_session.flush()

    assert await get_user_by_phone(db_session, original) is None


async def test_purging_revokes_sessions(db_session: AsyncSession, settings: Settings) -> None:
    doomed = await make_user(db_session, at=NYERI_TOWN)
    await auth_service.issue_tokens(db_session, user=doomed, settings=settings)
    doomed.deletion_requested_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.flush()

    await purge_deleted_accounts(db_session, settings=settings)

    sessions = (
        (await db_session.execute(select(DeviceSession).where(DeviceSession.user_id == doomed.id)))
        .scalars()
        .all()
    )
    assert sessions
    assert all(
        session.revoked_reason == SessionRevocationReason.account_deleted for session in sessions
    )


async def test_purging_leaves_the_audit_trail_intact(
    db_session: AsyncSession, settings: Settings
) -> None:
    """An audit log that vanishes with the account is useless for exactly the
    investigations it exists for."""
    from app.modules.moderation.models import AuditEvent

    doomed = await make_user(db_session, at=NYERI_TOWN)
    doomed.deletion_requested_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.flush()

    await purge_deleted_accounts(db_session, settings=settings)
    await db_session.flush()

    events = (
        (await db_session.execute(select(AuditEvent).where(AuditEvent.target_id == doomed.id)))
        .scalars()
        .all()
    )
    assert events


async def test_the_purge_is_batched(db_session: AsyncSession, settings: Settings) -> None:
    """One scheduled job must not hold a long transaction open over thousands of
    rows; whatever is left is picked up next run."""
    from app.modules.users.tasks import PURGE_BATCH_SIZE

    assert 0 < PURGE_BATCH_SIZE <= 1000


async def test_long_expired_sessions_are_deleted(
    db_session: AsyncSession, settings: Settings, user: User
) -> None:
    await auth_service.issue_tokens(db_session, user=user, settings=settings)
    await db_session.flush()
    stale = (
        (await db_session.execute(select(DeviceSession).where(DeviceSession.user_id == user.id)))
        .scalars()
        .first()
    )
    assert stale is not None
    stale.expires_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.flush()

    removed = await delete_expired_sessions(db_session)

    assert removed == 1


async def test_recently_expired_sessions_are_kept_for_investigation(
    db_session: AsyncSession, settings: Settings, user: User
) -> None:
    """Revoked rows are kept for a window so a reuse-detection event can still
    be looked at afterwards."""
    await auth_service.issue_tokens(db_session, user=user, settings=settings)
    await db_session.flush()
    session_row = (
        (await db_session.execute(select(DeviceSession).where(DeviceSession.user_id == user.id)))
        .scalars()
        .first()
    )
    assert session_row is not None
    session_row.expires_at = datetime.now(UTC) - timedelta(days=2)
    await db_session.flush()

    removed = await delete_expired_sessions(db_session)

    assert removed == 0


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
async def test_the_heartbeat_still_runs() -> None:
    """Kept now that real tasks exist: it is the only scheduled job with no
    dependencies, so it distinguishes "the worker is broken" from "a job is"."""
    assert (await heartbeat({"job_id": "t"})).endswith("+00:00")


def test_every_phase_1_task_is_registered() -> None:
    """A task missing from `functions` never runs, with no error anywhere."""
    names = {getattr(func, "__name__", "") for func in WorkerSettings.functions}

    assert {"heartbeat", "refresh_density", "roll_presence", "run_retention"} <= names


def test_the_density_refresh_is_scheduled_every_quarter_hour() -> None:
    """The cadence is how quickly the product notices a neighbourhood filling up."""
    density = [
        job
        for job in WorkerSettings.cron_jobs
        if getattr(job.coroutine, "__name__", "") == "refresh_density"
    ]

    assert len(density) == 1
    assert density[0].minute == {0, 15, 30, 45}


def test_the_density_refresh_runs_at_startup() -> None:
    """So a fresh deployment has density immediately, rather than labelling
    everyone generically until the next quarter hour."""
    density = next(
        job
        for job in WorkerSettings.cron_jobs
        if getattr(job.coroutine, "__name__", "") == "refresh_density"
    )

    assert density.run_at_startup is True


@pytest.mark.parametrize("task_name", ["roll_presence", "run_retention"])
def test_daily_jobs_run_after_local_midnight(task_name: str) -> None:
    """Scheduled in UTC, as everything is. 00:20 and 00:40 UTC are after 03:00 in
    Nairobi, so "today" has definitely turned over for every Kenyan user."""
    job = next(
        job
        for job in WorkerSettings.cron_jobs
        if getattr(job.coroutine, "__name__", "") == task_name
    )

    assert job.hour == {0}
    assert job.minute is not None
