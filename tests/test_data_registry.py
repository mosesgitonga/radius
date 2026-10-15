"""Every reference to a person has a written purge decision, and the purge does
what the decision says for the Phase 8 data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.main  # noqa: F401 — importing the app registers every model
from app.core.config import Settings
from app.core.models import Base
from app.modules.auth.models import DeviceSession
from app.modules.moderation import banlist
from app.modules.moderation.models import ModerationWarning
from app.modules.users.data_registry import PURGE_REGISTRY, Handling
from app.modules.users.models import Block, User, UserStatus, Vouch
from app.modules.users.tasks import purge_deleted_accounts
from tests.factories import NYERI_TOWN, make_user


def _columns_pointing_at_users() -> set[str]:
    return {
        f"{table.name}.{fk.parent.name}"
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name == "users"
    }


def test_every_reference_to_a_person_has_a_purge_decision() -> None:
    columns = _columns_pointing_at_users()
    missing = sorted(columns - PURGE_REGISTRY.keys())
    stale = sorted(PURGE_REGISTRY.keys() - columns)
    assert missing == [], f"Decide what the purge does to: {missing}"
    assert stale == [], f"No longer in the schema: {stale}"


def test_every_decision_gives_a_reason() -> None:
    for column, (handling, reason) in PURGE_REGISTRY.items():
        assert isinstance(handling, Handling)
        assert reason.strip(), column


async def test_the_purge_erases_blocks_vouches_warnings_and_device_hashes(
    db_session: AsyncSession, settings: Settings
) -> None:
    leaving = await make_user(db_session, at=NYERI_TOWN)
    other = await make_user(db_session, at=NYERI_TOWN)
    leaving_id = leaving.id
    db_session.add_all(
        [
            Block(blocker_id=leaving_id, blocked_id=other.id),
            Block(blocker_id=other.id, blocked_id=leaving_id),
            Vouch(voucher_id=leaving_id, vouchee_id=other.id),
            Vouch(voucher_id=other.id, vouchee_id=leaving_id),
            ModerationWarning(user_id=leaving_id, reason="Keep it civil"),
            DeviceSession(
                user_id=leaving_id,
                refresh_token_hash="x" * 64,
                family_id=leaving_id,
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_used_at=datetime.now(UTC),
                device_id_hash=banlist.device_hash("install-abc", settings),
            ),
        ]
    )
    leaving.status = UserStatus.deletion_pending
    leaving.restricted_until = datetime.now(UTC) + timedelta(days=3)
    leaving.restriction_reason = "Under review"
    leaving.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()

    result = await purge_deleted_accounts(db_session, settings=settings)
    assert result.accounts_purged == 1

    async def count(model: Any, *conditions: ColumnElement[bool]) -> int:
        return int(
            await db_session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
        )

    assert (
        await count(Block, (Block.blocker_id == leaving_id) | (Block.blocked_id == leaving_id)) == 0
    )
    assert (
        await count(Vouch, (Vouch.voucher_id == leaving_id) | (Vouch.vouchee_id == leaving_id)) == 0
    )
    assert await count(ModerationWarning, ModerationWarning.user_id == leaving_id) == 0
    assert (
        await count(
            DeviceSession,
            DeviceSession.device_id_hash.is_not(None),
            DeviceSession.user_id == leaving_id,
        )
        == 0
    )
    purged = await db_session.get(User, leaving_id)
    assert purged is not None
    await db_session.refresh(purged)
    assert purged.restricted_until is None
    assert purged.restriction_reason is None


async def test_the_export_covers_standing_vouches_warnings_and_reports(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    authenticate: Callable[[User], Awaitable[httpx.AsyncClient]],
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    other = await make_user(db_session, at=NYERI_TOWN)
    db_session.add_all(
        [
            Vouch(voucher_id=user.id, vouchee_id=other.id),
            ModerationWarning(user_id=user.id, reason="Keep it civil"),
        ]
    )
    await db_session.flush()
    await (await authenticate(user)).post(
        "/v1/reports", json={"target_type": "user", "target_id": str(other.id), "reason": "spam"}
    )
    export = (await (await authenticate(user)).get("/v1/me/export")).json()
    assert export["vouches"] == {"given_to": [str(other.id)], "received": 0}
    assert [w["reason"] for w in export["moderation"]["warnings_received"]] == ["Keep it civil"]
    assert export["moderation"]["reports_filed"][0]["target_id"] == str(other.id)
    assert "restricted_until" in export["account"]
