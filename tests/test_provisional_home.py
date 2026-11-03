"""Signing up away from home: a provisional home, full use, then the real one.

Decision M.10.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.users.models import User
from tests.factories import KARATINA, NAIROBI, NYERI_TOWN, Coordinates, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


def reading(at: Coordinates, **extra: Any) -> dict[str, Any]:
    return {**at.as_payload(), **extra}


async def newcomer(db_session: AsyncSession) -> User:
    """Signed up, no home yet."""
    return await make_user(db_session, created_days_ago=0, contact_unlocked=False)


async def test_away_from_home_gives_a_provisional_home_with_full_use(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    user = await newcomer(db_session)
    client = await authenticate(user)

    set_away = await client.put("/v1/me/location", json=reading(NAIROBI, at_home=False))
    assert set_away.status_code == 200, set_away.text
    me = (await (await authenticate(user)).get("/v1/me")).json()
    assert me["home_provisional"] is True
    assert me["home_confirm_by"] is not None
    confirm_by = datetime.fromisoformat(me["home_confirm_by"])
    assert timedelta(days=settings.provisional_home_days - 1) < confirm_by - datetime.now(UTC)

    posted = await (await authenticate(user)).post(
        "/v1/posts", json={"body": "Visiting for the week", "category": "general"}
    )
    assert posted.status_code == 201, posted.text
    # Nothing was proven by being there.
    await db_session.refresh(user)
    assert user.home_presence_mask == 0
    assert user.contact_unlocked_at is None


async def test_the_daily_check_records_nothing_while_provisional(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    await (await authenticate(user)).put("/v1/me/location", json=reading(NAIROBI, at_home=False))

    check = await (await authenticate(user)).post("/v1/me/presence", json=reading(NAIROBI))
    assert check.status_code == 200, check.text
    assert check.json()["at_home"] is False
    assert check.json()["days_this_week"] == 0
    await db_session.refresh(user)
    assert user.home_presence_mask == 0


async def test_the_real_home_replaces_it_without_a_cooldown(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    await (await authenticate(user)).put("/v1/me/location", json=reading(NAIROBI, at_home=False))

    real = await (await authenticate(user)).put("/v1/me/location", json=reading(NYERI_TOWN))
    assert real.status_code == 200, real.text
    me = (await (await authenticate(user)).get("/v1/me")).json()
    assert me["home_provisional"] is False
    assert me["home_confirm_by"] is None
    await db_session.refresh(user)
    assert user.home_changed_at is None
    assert user.home_presence_mask & 1 == 1


async def test_after_the_deadline_nothing_new_is_placed(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    user = await newcomer(db_session)
    await (await authenticate(user)).put("/v1/me/location", json=reading(NAIROBI, at_home=False))
    await db_session.refresh(user)
    user.home_set_at = datetime.now(UTC) - timedelta(days=settings.provisional_home_days + 1)
    await db_session.flush()

    refused = await (await authenticate(user)).post(
        "/v1/posts", json={"body": "Still visiting", "category": "general"}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "confirm_home"

    await (await authenticate(user)).put("/v1/me/location", json=reading(NYERI_TOWN))
    posted = await (await authenticate(user)).post(
        "/v1/posts", json={"body": "Home at last", "category": "general"}
    )
    assert posted.status_code == 201, posted.text


async def test_a_confirmed_home_cannot_be_made_provisional(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    response = await (await authenticate(user)).put(
        "/v1/me/location", json=reading(KARATINA, at_home=False)
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "home_confirmed"


async def test_at_home_is_the_default(db_session: AsyncSession, authenticate: Authenticate) -> None:
    user = await newcomer(db_session)
    response = await (await authenticate(user)).put("/v1/me/location", json=reading(NYERI_TOWN))
    assert response.status_code == 200, response.text
    assert (await (await authenticate(user)).get("/v1/me")).json()["home_provisional"] is False
