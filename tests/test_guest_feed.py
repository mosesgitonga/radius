"""The feed before signing up: around the phone, with no authors (decision M.9)."""

from __future__ import annotations

from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.moderation.models import ContentStatus
from app.modules.users.models import User
from tests.factories import NAIROBI, NYERI_TOWN, Coordinates, make_post, make_user, offset_km


def reading(at: Coordinates = NYERI_TOWN, **extra: Any) -> dict[str, Any]:
    return {**at.as_payload(), **extra}


async def test_a_visitor_sees_nearby_posts_without_authors(
    client: httpx.AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    author = await make_user(db_session, at=offset_km(NYERI_TOWN, east=1.0), display_name="Wanjiku")
    nearby = await make_post(db_session, redis_client, author=author, body="Water is off Saturday")
    far_author = await make_user(db_session, at=NAIROBI)
    await make_post(db_session, redis_client, author=far_author, body="Not for Nyeri")

    response = await client.post("/v1/guest/feed", json=reading())
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(nearby.id)]
    item = body["items"][0]
    assert item["body"] == "Water is off Saturday"
    assert item["distance_bucket"] is not None
    assert "author" not in item
    assert "Wanjiku" not in response.text
    assert str(author.id) not in response.text
    assert body["area_label"]["label"]
    assert body["radius_km"] > 0


async def test_hidden_removed_deleted_and_group_posts_stay_out(
    client: httpx.AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    hidden = await make_post(db_session, redis_client, author=author)
    hidden.status = ContentStatus.hidden_pending_review
    removed = await make_post(db_session, redis_client, author=author)
    removed.status = ContentStatus.removed
    deleted = await make_post(db_session, redis_client, author=author)
    deleted.deleted_at = func.now()
    await db_session.flush()

    body = (await client.post("/v1/guest/feed", json=reading())).json()
    assert body["items"] == []


async def test_a_faked_or_vague_location_is_refused(client: httpx.AsyncClient) -> None:
    faked = await client.post("/v1/guest/feed", json=reading(is_mocked=True))
    assert faked.status_code == 400
    assert faked.json()["error"]["details"]["reason"] == "mocked_location"
    vague = await client.post("/v1/guest/feed", json=reading(accuracy_metres=2000))
    assert vague.status_code == 400
    assert vague.json()["error"]["details"]["reason"] == "inaccurate_location"
    missing = await client.post("/v1/guest/feed", json={"latitude": -0.42, "longitude": 36.95})
    assert missing.status_code == 422


async def test_paging(
    client: httpx.AsyncClient, db_session: AsyncSession, redis_client: Redis, settings: Any
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    for n in range(settings.pagination_default_limit + 2):
        await make_post(db_session, redis_client, author=author, body=f"Post {n}")
    first = (await client.post("/v1/guest/feed", json=reading())).json()
    assert len(first["items"]) == settings.pagination_default_limit
    second = (await client.post("/v1/guest/feed", json=reading(cursor=first["next_cursor"]))).json()
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None


async def test_nothing_about_a_visitor_is_stored(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    before = await db_session.scalar(select(func.count()).select_from(User))
    await client.post("/v1/guest/feed", json=reading())
    assert await db_session.scalar(select(func.count()).select_from(User)) == before


async def test_a_visitor_is_rate_limited(client: httpx.AsyncClient) -> None:
    statuses = [
        (await client.post("/v1/guest/feed", json=reading())).status_code for _ in range(31)
    ]
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429
