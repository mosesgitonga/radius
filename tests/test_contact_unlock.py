"""Home is found by GPS only, and a newcomer proves it before contacting strangers.

Decision M.7. A reading the phone marks as faked, or one too vague, is refused. The
daily check records one bit a day and never moves home. Someone new may post,
comment and react at once, but starts no conversation with a stranger — and
invites no stranger anywhere — until at home on `CONTACT_UNLOCK_DAYS` of the last
`CONTACT_UNLOCK_WINDOW_DAYS` days.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import User
from tests.factories import NAIROBI, NYERI_TOWN, make_user, offset_km

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def newcomer(db_session: AsyncSession, **kwargs: Any) -> User:
    """Someone at home in Nyeri who has not yet proven it."""
    at = kwargs.pop("at", NYERI_TOWN)
    return await make_user(db_session, at=at, created_days_ago=30, contact_unlocked=False, **kwargs)


def reading(at: Any = NYERI_TOWN, **extra: Any) -> dict[str, Any]:
    return {**at.as_payload(), **extra}


async def present_on_previous_days(db_session: AsyncSession, user: User, days: int) -> None:
    """As if the daily check had found them at home on each of the last `days` days."""
    user.home_presence_mask = (1 << days) - 1
    user.home_presence_updated_on = datetime.now(UTC).date() - timedelta(days=1)
    await db_session.flush()


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------
async def test_a_faked_reading_is_refused(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await make_user(db_session)
    response = await (await authenticate(user)).put("/v1/me/location", json=reading(is_mocked=True))
    assert response.status_code == 400
    assert response.json()["error"]["details"]["reason"] == "mocked_location"


async def test_a_vague_reading_is_refused(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await make_user(db_session)
    response = await (await authenticate(user)).put(
        "/v1/me/location", json=reading(accuracy_metres=1500)
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["reason"] == "inaccurate_location"


async def test_a_reading_must_say_how_accurate_it_is(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await make_user(db_session)
    payload = {"latitude": NYERI_TOWN.latitude, "longitude": NYERI_TOWN.longitude}
    assert (
        await (await authenticate(user)).put("/v1/me/location", json=payload)
    ).status_code == 422


# ---------------------------------------------------------------------------
# The daily check
# ---------------------------------------------------------------------------
async def test_the_daily_check_needs_a_home(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await make_user(db_session)
    assert (
        await (await authenticate(user)).post("/v1/me/presence", json=reading())
    ).status_code == 409


async def test_the_daily_check_records_home_and_away_and_never_moves_home(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    home = user.home_cell_res9
    client = await authenticate(user)
    assert (await client.get("/v1/me")).json()["presence_check_due"] is True

    away = (await client.post("/v1/me/presence", json=reading(NAIROBI))).json()
    assert away["at_home"] is False
    await db_session.refresh(user)
    assert user.home_cell_res9 == home

    there = (
        await (await authenticate(user)).post(
            "/v1/me/presence", json=reading(offset_km(NYERI_TOWN, east=0.3))
        )
    ).json()
    assert there["at_home"] is True
    assert there["days_this_week"] == 1
    me = (await (await authenticate(user)).get("/v1/me")).json()
    assert me["presence_check_due"] is False
    assert me["can_contact_strangers"] is False


async def test_four_days_in_a_row_unlock_contact(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    await present_on_previous_days(db_session, user, 3)

    outcome = (await (await authenticate(user)).post("/v1/me/presence", json=reading())).json()
    assert outcome["days_this_week"] == 4
    assert outcome["contact_unlock_window_days"] == 7
    assert outcome["can_contact_strangers"] is True
    assert (await (await authenticate(user)).get("/v1/me")).json()["can_contact_strangers"] is True


async def test_four_days_in_a_week_unlock_even_with_gaps(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    # Bit 0 is yesterday, when they were away; three of the five days before, at home.
    user.home_presence_mask = 0b101010
    user.home_presence_updated_on = datetime.now(UTC).date() - timedelta(days=1)
    await db_session.flush()

    outcome = (await (await authenticate(user)).post("/v1/me/presence", json=reading())).json()
    assert outcome["days_this_week"] == 4
    assert outcome["can_contact_strangers"] is True


async def test_days_older_than_a_week_do_not_count(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    # Yesterday is bit 0. Bits 6 and up are more than six days before today, so
    # only bits 0-5 still fall in the week: two days at home, plus today.
    user.home_presence_mask = 0b1111000011
    user.home_presence_updated_on = datetime.now(UTC).date() - timedelta(days=1)
    await db_session.flush()

    outcome = (await (await authenticate(user)).post("/v1/me/presence", json=reading())).json()
    assert outcome["days_this_week"] == 3
    assert outcome["can_contact_strangers"] is False


# ---------------------------------------------------------------------------
# What a newcomer may and may not do
# ---------------------------------------------------------------------------
async def test_a_newcomer_may_post_comment_and_react(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    client = await authenticate(user)
    post = await client.post(
        "/v1/posts", json={"body": "Water is off on Saturday", "category": "alert"}
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["post"]["id"]
    comment = await (await authenticate(user)).post(
        f"/v1/posts/{post_id}/comments", json={"body": "From 9am"}
    )
    assert comment.status_code == 201, comment.text
    reaction = await (await authenticate(user)).put(
        f"/v1/posts/{post_id}/reaction", json={"kind": "helpful"}
    )
    assert reaction.status_code == 200, reaction.text


async def test_a_newcomer_cannot_message_a_stranger_until_unlocked(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    stranger = await make_user(db_session, at=NYERI_TOWN)
    conversation = (
        await (await authenticate(user)).post(
            "/v1/conversations/direct", json={"user_id": str(stranger.id)}
        )
    ).json()

    refused = await (await authenticate(user)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Hello neighbour"}
    )
    assert refused.status_code == 403
    details = refused.json()["error"]["details"]
    assert details == {
        "reason": "contact_locked",
        "days_needed": 4,
        "window_days": 7,
        "days_so_far": 0,
    }

    await present_on_previous_days(db_session, user, 3)
    await (await authenticate(user)).post("/v1/me/presence", json=reading())
    sent = await (await authenticate(user)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Hello neighbour"}
    )
    assert sent.status_code == 201, sent.text


async def test_a_newcomer_may_reply_to_someone_who_wrote_first(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    neighbour = await make_user(db_session, at=NYERI_TOWN)
    conversation = (
        await (await authenticate(neighbour)).post(
            "/v1/conversations/direct", json={"user_id": str(user.id)}
        )
    ).json()
    await (await authenticate(neighbour)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Welcome to the area!"}
    )

    reply = await (await authenticate(user)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Asante!"}
    )
    assert reply.status_code == 201, reply.text


async def test_a_newcomer_cannot_ask_a_seller(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller = await make_user(db_session, at=NYERI_TOWN, created_days_ago=30)
    listing = (
        await (await authenticate(seller)).post(
            "/v1/listings",
            json={
                "title": "Gas cooker",
                "category": "household",
                "condition": "good",
                "pricing": "fixed",
                "price_kes": 4500,
            },
        )
    ).json()
    buyer = await newcomer(db_session)
    conversation = (
        await (await authenticate(buyer)).post(f"/v1/listings/{listing['id']}/chat")
    ).json()

    refused = await (await authenticate(buyer)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Still available?"}
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["reason"] == "contact_locked"


async def test_a_newcomer_cannot_invite_strangers_to_a_group_chat(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session)
    stranger = await make_user(db_session, at=NYERI_TOWN)
    response = await (await authenticate(user)).post(
        "/v1/conversations/groups",
        json={"title": "Estate committee", "member_ids": [str(stranger.id)]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "contact_locked"


async def test_a_verified_neighbour_is_always_unlocked(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await newcomer(db_session, is_verified_neighbour=True)
    stranger = await make_user(db_session, at=NYERI_TOWN)
    conversation = (
        await (await authenticate(user)).post(
            "/v1/conversations/direct", json={"user_id": str(stranger.id)}
        )
    ).json()
    sent = await (await authenticate(user)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Hello"}
    )
    assert sent.status_code == 201, sent.text


async def test_moving_home_locks_contact_again(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)  # unlocked
    user.home_changed_at = datetime.now(UTC) - timedelta(
        days=settings.home_change_cooldown_days + 1
    )
    await db_session.flush()

    moved = await (await authenticate(user)).put(
        "/v1/me/location", json=reading(offset_km(NYERI_TOWN, north=8))
    )
    assert moved.status_code == 200, moved.text
    assert (await (await authenticate(user)).get("/v1/me")).json()["can_contact_strangers"] is False


async def test_the_purge_clears_the_unlock(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    from app.modules.users.tasks import purge_deleted_accounts

    user = await make_user(db_session, at=NYERI_TOWN)
    export = (await (await authenticate(user)).get("/v1/me/export")).json()
    assert export["account"]["contact_unlocked_at"] is not None

    user.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()
    await purge_deleted_accounts(db_session, settings=settings)
    await db_session.refresh(user)
    assert user.contact_unlocked_at is None
    assert user.presence_checked_on is None
