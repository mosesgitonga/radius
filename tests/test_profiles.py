"""Profiles: a bio and a cover photo, and a person's posts, listings and services.

A profile's posts follow the feed: a neighbour whose feed could show them sees them,
someone further away sees the profile without them. Listings and services are open
to anyone who can open the profile, because they are meant to be found.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.storage import MemoryStorageProvider
from app.modules.media.models import MediaPurpose
from app.modules.moderation.models import ContentStatus
from app.modules.users.models import User
from app.modules.users.service import block_user
from tests.factories import NAIROBI, NYERI_TOWN, make_post, make_ready_image, make_user, offset_km

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def person(db_session: AsyncSession, **kwargs: Any) -> User:
    at = kwargs.pop("at", NYERI_TOWN)
    return await make_user(db_session, at=at, created_days_ago=kwargs.pop("age", 30), **kwargs)


# ---------------------------------------------------------------------------
# Bio and cover
# ---------------------------------------------------------------------------
async def test_a_bio_is_set_tidied_shown_and_cleared(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await person(db_session)
    viewer = await person(db_session)
    client = await authenticate(owner)

    response = await client.patch(
        "/v1/me", json={"bio": "  Plumber in Kamakwa.  \n\n  Weekdays from 7am. "}
    )
    assert response.status_code == 200, response.text
    assert response.json()["bio"] == "Plumber in Kamakwa.\nWeekdays from 7am."

    profile = (await (await authenticate(viewer)).get(f"/v1/users/{owner.id}")).json()
    assert profile["bio"] == "Plumber in Kamakwa.\nWeekdays from 7am."

    cleared = await (await authenticate(owner)).patch("/v1/me", json={"bio": ""})
    assert cleared.json()["bio"] is None


async def test_a_bio_is_at_most_160_characters(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    assert (await client.patch("/v1/me", json={"bio": "x" * 160})).status_code == 200
    assert (await client.patch("/v1/me", json={"bio": "x" * 161})).status_code == 422


async def test_a_bio_change_is_audited_without_its_text(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    from sqlalchemy import select

    from app.modules.moderation.models import AuditEvent

    owner = await person(db_session)
    await (await authenticate(owner)).patch("/v1/me", json={"bio": "Mama fua, Ruring'u"})

    events = (
        (await db_session.execute(select(AuditEvent).where(AuditEvent.actor_id == owner.id)))
        .scalars()
        .all()
    )
    changed = [e for e in events if str(e.action) == "profile_updated"]
    assert changed and changed[-1].context == {"fields": ["bio"]}
    assert "Mama fua" not in str([e.context for e in events])


async def test_a_cover_photo_is_set_shown_and_cleared(
    db_session: AsyncSession, storage: MemoryStorageProvider, authenticate: Authenticate
) -> None:
    owner = await person(db_session)
    viewer = await person(db_session)
    cover = await make_ready_image(db_session, storage, owner=owner, purpose=MediaPurpose.cover)
    client = await authenticate(owner)

    response = await client.put("/v1/me/cover", json={"media_id": str(cover.id)})
    assert response.status_code == 200, response.text
    me = (await client.get("/v1/me")).json()
    # Shown wide, so it gets the post sizes rather than the one small avatar size.
    assert me["cover"] is not None and len(me["cover"]) > 1
    profile = (await (await authenticate(viewer)).get(f"/v1/users/{owner.id}")).json()
    assert profile["cover"] is not None

    await (await authenticate(owner)).put("/v1/me/cover", json={"media_id": None})
    assert (await (await authenticate(owner)).get("/v1/me")).json()["cover"] is None


async def test_an_avatar_upload_cannot_be_used_as_a_cover(
    db_session: AsyncSession, storage: MemoryStorageProvider, authenticate: Authenticate
) -> None:
    owner = await person(db_session)
    avatar = await make_ready_image(db_session, storage, owner=owner, purpose=MediaPurpose.avatar)
    response = await (await authenticate(owner)).put(
        "/v1/me/cover", json={"media_id": str(avatar.id)}
    )
    assert response.status_code >= 400


# ---------------------------------------------------------------------------
# Posts on a profile
# ---------------------------------------------------------------------------
async def test_a_neighbour_sees_all_of_a_persons_posts(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await person(db_session)
    neighbour = await person(db_session, at=offset_km(NYERI_TOWN, north=1))
    first = await make_post(
        db_session, redis_client, author=author, body="Water is off on Saturday"
    )
    second = await make_post(db_session, redis_client, author=author, body="Lost: a brown dog")
    await make_post(db_session, redis_client, author=neighbour, body="Not theirs")

    client = await authenticate(neighbour)
    profile = (await client.get(f"/v1/users/{author.id}")).json()
    assert profile["posts_visible"] is True

    page = (await client.get(f"/v1/users/{author.id}/posts")).json()
    assert [p["id"] for p in page["items"]] == [str(second.id), str(first.id)]


async def test_someone_far_away_sees_the_profile_but_not_the_posts(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await person(db_session)
    await make_post(db_session, redis_client, author=author)
    stranger = await person(db_session, at=NAIROBI)

    client = await authenticate(stranger)
    profile = await client.get(f"/v1/users/{author.id}")
    assert profile.status_code == 200
    assert profile.json()["posts_visible"] is False
    assert (await client.get(f"/v1/users/{author.id}/posts")).json()["items"] == []


async def test_my_own_profile_shows_my_held_posts_but_not_to_others(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await person(db_session)
    neighbour = await person(db_session)
    held = await make_post(db_session, redis_client, author=author)
    held.status = ContentStatus.hidden_pending_review
    await db_session.flush()

    mine = (await (await authenticate(author)).get(f"/v1/users/{author.id}/posts")).json()
    assert [p["id"] for p in mine["items"]] == [str(held.id)]
    theirs = (await (await authenticate(neighbour)).get(f"/v1/users/{author.id}/posts")).json()
    assert theirs["items"] == []


async def test_a_block_hides_the_profile_and_every_list(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await person(db_session)
    blocked = await person(db_session)
    await make_post(db_session, redis_client, author=author)
    await block_user(db_session, blocker=author, blocked_id=blocked.id)
    await db_session.flush()

    client = await authenticate(blocked)
    for path in ("", "/posts", "/listings", "/services"):
        response = await client.get(f"/v1/users/{author.id}{path}")
        # The same answer as for nobody at all, so a block cannot be confirmed.
        assert response.status_code == 404, path


# ---------------------------------------------------------------------------
# What they sell and offer
# ---------------------------------------------------------------------------
async def test_a_profile_lists_open_listings_only(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller = await person(db_session)
    stranger = await person(db_session, at=NAIROBI)
    client = await authenticate(seller)

    def body(title: str) -> dict[str, Any]:
        return {
            "title": title,
            "category": "furniture",
            "condition": "good",
            "pricing": "fixed",
            "price_kes": 5000,
        }

    sofa = (await client.post("/v1/listings", json=body("Sofa, grey"))).json()
    lamp = (
        await (await authenticate(seller)).post("/v1/listings", json=body("Reading lamp"))
    ).json()
    await (await authenticate(seller)).put(
        f"/v1/listings/{lamp['id']}/availability", json={"availability": "sold"}
    )

    # Open to anyone who can open the profile, near or far.
    page = (await (await authenticate(stranger)).get(f"/v1/users/{seller.id}/listings")).json()
    assert [item["id"] for item in page["items"]] == [sofa["id"]]


async def test_a_profile_lists_open_service_posts_only(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session)
    viewer = await person(db_session)
    client = await authenticate(provider)
    offering = (
        await client.post(
            "/v1/services",
            json={"kind": "offering", "category": "plumbing", "title": "Plumber, 10 years"},
        )
    ).json()
    request = (
        await (await authenticate(provider)).post(
            "/v1/services",
            json={"kind": "seeking", "category": "laundry", "title": "Mama fua on Saturdays"},
        )
    ).json()
    await (await authenticate(provider)).put(
        f"/v1/services/{request['id']}/open", json={"open": False}
    )

    page = (await (await authenticate(viewer)).get(f"/v1/users/{provider.id}/services")).json()
    assert [item["id"] for item in page["items"]] == [offering["id"]]


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def test_the_bio_is_exported_and_the_purge_clears_bio_and_cover(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
    settings: Any,
) -> None:
    from app.modules.users.tasks import purge_deleted_accounts

    owner = await person(db_session)
    cover = await make_ready_image(db_session, storage, owner=owner, purpose=MediaPurpose.cover)
    client = await authenticate(owner)
    await client.patch("/v1/me", json={"bio": "Tailor, school uniforms"})
    await (await authenticate(owner)).put("/v1/me/cover", json={"media_id": str(cover.id)})

    export = (await (await authenticate(owner)).get("/v1/me/export")).json()
    assert export["account"]["bio"] == "Tailor, school uniforms"

    await db_session.refresh(owner)
    owner.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()
    await purge_deleted_accounts(db_session, settings=settings)

    await db_session.refresh(owner)
    assert owner.bio is None
    assert owner.cover_media_id is None


async def test_my_own_profile_has_no_distance_to_myself(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await person(db_session)
    neighbour = await person(db_session)
    mine = (await (await authenticate(owner)).get(f"/v1/users/{owner.id}")).json()
    assert mine["distance_bucket"] is None
    theirs = (await (await authenticate(neighbour)).get(f"/v1/users/{owner.id}")).json()
    assert theirs["distance_bucket"] == "under_1km"
