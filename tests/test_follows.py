"""Follows: following a service, from anywhere, never a person.

Decision C.5. A follower sees posts made as the service in their circle feed,
wherever either side lives; never the provider's own posts. Nobody sees who
follows them, only how many.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.follows import service as follows_service
from app.modules.follows.models import Follow
from app.modules.services.models import ServicePost
from app.modules.users.models import User
from app.modules.users.service import block_user
from app.modules.users.tasks import purge_deleted_accounts
from tests.factories import NAIROBI, NYERI_TOWN, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def person(db_session: AsyncSession, **kwargs: Any) -> User:
    at = kwargs.pop("at", NYERI_TOWN)
    return await make_user(db_session, at=at, created_days_ago=kwargs.pop("age", 60), **kwargs)


async def offering(authenticate: Authenticate, provider: User, **fields: Any) -> dict[str, Any]:
    body = {"kind": "offering", "category": "plumbing", "title": "Plumber, 10 years", **fields}
    response = await (await authenticate(provider)).post("/v1/services", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def follow(authenticate: Authenticate, user: User, service_id: str) -> httpx.Response:
    return await (await authenticate(user)).post(
        "/v1/follows", json={"entity_type": "service_provider", "entity_id": service_id}
    )


async def test_following_a_service_from_another_town(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session)
    follower = await person(db_session, at=NAIROBI)
    service = await offering(authenticate, provider)

    followed = await follow(authenticate, follower, service["id"])
    assert followed.status_code == 201, followed.text
    body = followed.json()
    assert body["follower_count"] == 1
    assert body["is_following"] is True
    assert body["service"]["title"] == "Plumber, 10 years"

    again = await follow(authenticate, follower, service["id"])
    assert again.status_code == 201
    assert await db_session.scalar(select(func.count()).select_from(Follow)) == 1

    shown = (await (await authenticate(follower)).get(f"/v1/services/{service['id']}")).json()
    assert shown["follower_count"] == 1
    assert shown["is_following"] is True
    mine = (await (await authenticate(follower)).get("/v1/me/follows")).json()
    assert [item["entity_id"] for item in mine["items"]] == [service["id"]]

    left = await (await authenticate(follower)).delete(
        f"/v1/follows/service_provider/{service['id']}"
    )
    assert left.status_code == 204
    assert (await (await authenticate(follower)).get("/v1/me/follows")).json()["items"] == []
    # Again is harmless.
    assert (
        await (await authenticate(follower)).delete(f"/v1/follows/service_provider/{service['id']}")
    ).status_code == 204


@pytest.mark.parametrize("entity_type", ["user", "person", "business", "group"])
async def test_only_services_can_be_followed(
    db_session: AsyncSession, authenticate: Authenticate, entity_type: str
) -> None:
    someone = await person(db_session)
    follower = await person(db_session)
    response = await (await authenticate(follower)).post(
        "/v1/follows", json={"entity_type": entity_type, "entity_id": str(someone.id)}
    )
    assert response.status_code == 422


async def test_a_person_cannot_be_followed_through_a_service_id(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    someone = await person(db_session)
    response = await follow(authenticate, await person(db_session), str(someone.id))
    assert response.status_code == 404


async def test_only_an_offering_can_be_followed(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    asker = await person(db_session)
    need = await offering(authenticate, asker, kind="seeking", title="Need a plumber")
    assert (await follow(authenticate, await person(db_session), need["id"])).status_code == 404


async def test_you_cannot_follow_your_own_service(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session)
    service = await offering(authenticate, provider)
    assert (await follow(authenticate, provider, service["id"])).status_code == 400


async def test_follows_need_signing_in(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/me/follows")).status_code == 401
    assert (
        await client.post(
            "/v1/follows",
            json={
                "entity_type": "service_provider",
                "entity_id": "00000000-0000-0000-0000-000000000001",
            },
        )
    ).status_code == 401


async def test_posts_made_as_the_service_reach_followers_anywhere(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session)
    follower = await person(db_session, at=NAIROBI)
    service = await offering(authenticate, provider)
    await follow(authenticate, follower, service["id"])
    provider_client = await authenticate(provider)

    as_service = await provider_client.post(
        "/v1/posts",
        json={
            "body": "Free tank inspection this Saturday",
            "category": "general",
            "service_post_id": service["id"],
        },
    )
    assert as_service.status_code == 201, as_service.text
    assert as_service.json()["post"]["service_post_id"] == service["id"]
    personal = await (await authenticate(provider)).post(
        "/v1/posts", json={"body": "My cousin's wedding was lovely", "category": "general"}
    )
    assert personal.status_code == 201
    kept = await (await authenticate(provider)).post(
        "/v1/posts",
        json={
            "body": "Discount for Kamakwa only",
            "category": "general",
            "service_post_id": service["id"],
            "audience": "neighbours_only",
        },
    )
    assert kept.status_code == 201

    page = (await (await authenticate(follower)).get("/v1/feed/circle")).json()
    assert [item["id"] for item in page["items"]] == [as_service.json()["post"]["id"]]
    assert page["items"][0]["distance_bucket"] is None


async def test_you_can_post_only_as_your_own_offering(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session)
    other = await person(db_session)
    service = await offering(authenticate, provider)
    need = await offering(authenticate, other, kind="seeking", title="Need a plumber")
    for who, service_id in [(other, service["id"]), (other, need["id"])]:
        response = await (await authenticate(who)).post(
            "/v1/posts",
            json={"body": "Hello", "category": "general", "service_post_id": service_id},
        )
        assert response.status_code == 400


async def test_a_blocked_providers_service_disappears(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session)
    follower = await person(db_session)
    service = await offering(authenticate, provider)
    await follow(authenticate, follower, service["id"])
    await (await authenticate(provider)).post(
        "/v1/posts",
        json={"body": "Open today", "category": "general", "service_post_id": service["id"]},
    )

    await block_user(db_session, blocker=follower, blocked_id=provider.id)
    await db_session.flush()
    assert (await (await authenticate(follower)).get("/v1/me/follows")).json()["items"] == []
    assert (await (await authenticate(follower)).get("/v1/feed/circle")).json()["items"] == []
    assert (await follow(authenticate, follower, service["id"])).status_code == 404


async def test_follows_have_a_daily_limit(
    db_session: AsyncSession,
    authenticate: Authenticate,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "follows_per_day", 2)
    follower = await person(db_session)
    services = [
        await offering(authenticate, await person(db_session), title=f"Plumber {n}")
        for n in range(3)
    ]
    statuses = [(await follow(authenticate, follower, s["id"])).status_code for s in services]
    assert statuses == [201, 201, 429]


async def test_follows_of_a_deleted_service_are_cleared(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session)
    follower = await person(db_session)
    service = await offering(authenticate, provider)
    await follow(authenticate, follower, service["id"])
    deleted = await (await authenticate(provider)).delete(f"/v1/services/{service['id']}")
    assert deleted.status_code == 204

    assert (await (await authenticate(follower)).get("/v1/me/follows")).json()["items"] == []
    assert await follows_service.delete_orphaned_follows(db_session) == 1
    assert await db_session.scalar(select(func.count()).select_from(Follow)) == 0


async def test_export_and_purge(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    provider = await person(db_session)
    follower = await person(db_session)
    service = await offering(authenticate, provider)
    await follow(authenticate, follower, service["id"])

    export = (await (await authenticate(follower)).get("/v1/me/export")).json()["follows"]
    assert [item["entity_id"] for item in export["following"]] == [service["id"]]
    # The provider's export does not say who follows them.
    theirs = (await (await authenticate(provider)).get("/v1/me/export")).json()["follows"]
    assert theirs["following"] == []

    follower.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()
    await purge_deleted_accounts(db_session, settings=settings)
    assert await db_session.scalar(select(func.count()).select_from(Follow)) == 0
    assert await db_session.get(ServicePost, service["id"]) is not None
