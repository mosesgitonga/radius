"""People nearby: opt-in, proven on both sides, the feed's reach from home.

Decision M.8.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.users.models import User, UserStatus
from app.modules.users.service import block_user
from app.modules.users.tasks import purge_deleted_accounts
from tests.factories import NAIROBI, NYERI_TOWN, make_user, offset_km

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def person(db_session: AsyncSession, *, listed: bool = True, **kwargs: Any) -> User:
    user = await make_user(
        db_session, at=kwargs.pop("at", NYERI_TOWN), created_days_ago=30, **kwargs
    )
    user.show_in_nearby = listed
    await db_session.flush()
    return user


async def nearby_ids(authenticate: Authenticate, viewer: User) -> list[str]:
    response = await (await authenticate(viewer)).get("/v1/people/nearby")
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def test_lists_neighbours_who_opted_in(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    viewer = await person(db_session, listed=False)
    listed = await person(db_session, at=offset_km(NYERI_TOWN, east=1.0), display_name="Wanjiku")
    await person(db_session, listed=False, at=offset_km(NYERI_TOWN, north=1.0))
    await person(db_session, at=NAIROBI)

    response = await (await authenticate(viewer)).get("/v1/people/nearby")
    assert response.status_code == 200, response.text
    (item,) = response.json()["items"]
    assert item["id"] == str(listed.id)
    assert item["display_name"] == "Wanjiku"
    assert item["area_label"] is not None
    assert item["distance_bucket"] is not None
    assert "last_active_at" not in item


async def test_nobody_is_listed_until_they_turn_it_on(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    viewer = await person(db_session)
    other = await person(db_session, listed=False)
    assert await nearby_ids(authenticate, viewer) == []
    assert (await (await authenticate(other)).get("/v1/me")).json()["show_in_nearby"] is False

    turned_on = await (await authenticate(other)).patch("/v1/me", json={"show_in_nearby": True})
    assert turned_on.json()["show_in_nearby"] is True
    assert await nearby_ids(authenticate, viewer) == [str(other.id)]

    await (await authenticate(other)).patch("/v1/me", json={"show_in_nearby": False})
    assert await nearby_ids(authenticate, viewer) == []


async def test_an_unproven_viewer_cannot_look(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    newcomer = await person(db_session, contact_unlocked=False)
    await person(db_session)
    response = await (await authenticate(newcomer)).get("/v1/people/nearby")
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "contact_locked"


async def test_an_unproven_person_is_not_listed(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    viewer = await person(db_session)
    await person(db_session, contact_unlocked=False)
    verified = await person(db_session, contact_unlocked=False, is_verified_neighbour=True)
    assert await nearby_ids(authenticate, viewer) == [str(verified.id)]


async def test_blocked_suspended_and_leaving_people_are_left_out(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    viewer = await person(db_session)
    blocked = await person(db_session)
    blocker = await person(db_session)
    await person(db_session, status=UserStatus.suspended)
    leaving = await person(db_session)
    leaving.deletion_requested_at = datetime.now(UTC)
    await block_user(db_session, blocker=viewer, blocked_id=blocked.id)
    await block_user(db_session, blocker=blocker, blocked_id=viewer.id)
    await db_session.flush()
    assert await nearby_ids(authenticate, viewer) == []


async def test_exploring_does_not_move_the_list(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    viewer = await person(db_session)
    home_neighbour = await person(db_session)
    await person(db_session, at=NAIROBI)
    explored = await (await authenticate(viewer)).post("/v1/me/explore", json=NAIROBI.as_payload())
    assert explored.status_code in (200, 201), explored.text
    assert await nearby_ids(authenticate, viewer) == [str(home_neighbour.id)]


async def test_people_nearby_needs_signing_in(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/people/nearby")).status_code == 401


async def test_the_setting_is_exported_and_reset_on_purge(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    user = await person(db_session)
    export = (await (await authenticate(user)).get("/v1/me/export")).json()
    assert export["account"]["show_in_nearby"] is True

    user.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()
    await purge_deleted_accounts(db_session, settings=settings)
    await db_session.refresh(user)
    assert user.show_in_nearby is False
