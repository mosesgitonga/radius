"""The neighbourhood feed, end to end.

Phase 2's acceptance criterion, verbatim: "two test users in the same area see
each other's posts, a user far away doesn't, blocked users' content never appears,
and uploaded images come back without EXIF." Each clause has its own section.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.storage import MemoryStorageProvider
from app.modules.feed.models import Post
from app.modules.media.images import has_exif, read_gps_tags
from app.modules.moderation.models import ContentStatus
from app.modules.users.models import User
from app.modules.users.service import block_user
from tests.factories import (
    KARATINA,
    NAIROBI,
    NYERI_TOWN,
    make_post,
    make_ready_image,
    make_user,
    offset_km,
    set_cell_density,
)

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def feed_bodies(client: httpx.AsyncClient) -> list[str]:
    response = await client.get("/v1/feed")
    assert response.status_code == 200, response.text
    return [item["body"] for item in response.json()["items"]]


# ---------------------------------------------------------------------------
# "Two test users in the same area see each other's posts"
# ---------------------------------------------------------------------------
async def test_neighbours_see_each_others_posts(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    wanjiku = await make_user(db_session, at=NYERI_TOWN, display_name="Wanjiku")
    kamau = await make_user(db_session, at=offset_km(NYERI_TOWN, north=1.0), display_name="Kamau")
    await make_post(db_session, redis_client, author=wanjiku, body="Maji yamerudi Kamakwa")
    await make_post(db_session, redis_client, author=kamau, body="Lost keys near the stage")

    wanjiku_sees = await feed_bodies(await authenticate(wanjiku))
    kamau_sees = await feed_bodies(await authenticate(kamau))

    assert "Lost keys near the stage" in wanjiku_sees
    assert "Maji yamerudi Kamakwa" in kamau_sees


async def test_my_own_posts_appear_in_my_feed(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    await make_post(db_session, redis_client, author=author, body="Mine")

    assert "Mine" in await feed_bodies(await authenticate(author))


async def test_the_feed_is_newest_first(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    for index in range(3):
        await make_post(db_session, redis_client, author=author, body=f"Post {index}")

    bodies = await feed_bodies(await authenticate(author))

    assert bodies[:3] == ["Post 2", "Post 1", "Post 0"]


async def test_the_feed_paginates_without_repeats(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    for index in range(7):
        await make_post(db_session, redis_client, author=author, body=f"Post {index}")
    client = await authenticate(author)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        url = "/v1/feed?limit=3" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url)).json()
        seen.extend(item["body"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 7
    assert len(set(seen)) == 7


# ---------------------------------------------------------------------------
# "A user far away doesn't"
# ---------------------------------------------------------------------------
async def test_a_user_far_away_does_not_see_the_post(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Nairobi is well beyond even the 15 km maximum from Nyeri."""
    author = await make_user(db_session, at=NYERI_TOWN)
    stranger = await make_user(db_session, at=NAIROBI)
    await make_post(db_session, redis_client, author=author, body="Nyeri only")

    assert "Nyeri only" not in await feed_bodies(await authenticate(stranger))


async def test_the_boundary_follows_the_adaptive_radius(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """At launch density the radius is 15 km. A dense neighbourhood shrinks it, and
    a post that was in range is then out of it — the radius genuinely filters."""
    author = await make_user(db_session, at=offset_km(NYERI_TOWN, north=5.0))
    viewer = await make_user(db_session, at=NYERI_TOWN)
    await make_post(db_session, redis_client, author=author, body="Five kilometres north")

    client = await authenticate(viewer)
    assert "Five kilometres north" in await feed_bodies(client)

    await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=600, rings=1)
    await redis_client.flushdb()

    assert "Five kilometres north" not in await feed_bodies(client)


async def test_karatina_and_nyeri_have_separate_feeds(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Twenty-two kilometres apart, beyond the 15 km maximum."""
    nyeri = await make_user(db_session, at=NYERI_TOWN)
    karatina = await make_user(db_session, at=KARATINA)
    await make_post(db_session, redis_client, author=karatina, body="Karatina market news")

    assert "Karatina market news" not in await feed_bodies(await authenticate(nyeri))


async def test_explore_mode_shows_the_explored_area(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    nyeri = await make_user(db_session, at=NYERI_TOWN)
    karatina = await make_user(db_session, at=KARATINA)
    await make_post(db_session, redis_client, author=karatina, body="Karatina market news")
    client = await authenticate(nyeri)

    await client.post("/v1/me/explore", json=KARATINA.as_payload())
    explored = await client.get("/v1/feed?anchor=explore")

    assert "Karatina market news" in [item["body"] for item in explored.json()["items"]]


async def test_posting_while_exploring_still_posts_at_home(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Decision 1.1, rule 1 — the rule that stops someone dropping posts into a
    neighbourhood they do not live in."""
    author = await make_user(db_session, at=NYERI_TOWN)
    karatina_local = await make_user(db_session, at=KARATINA)
    client = await authenticate(author)

    await client.post("/v1/me/explore", json=KARATINA.as_payload())
    created = await client.post(
        "/v1/posts", json={"body": "Posted while exploring", "category": "general"}
    )
    assert created.status_code == 201

    post = (
        await db_session.execute(select(Post).where(Post.body == "Posted while exploring"))
    ).scalar_one()
    assert post.cell_res9 == author.home_cell_res9
    assert "Posted while exploring" not in await feed_bodies(await authenticate(karatina_local))


# ---------------------------------------------------------------------------
# "Blocked users' content never appears"
# ---------------------------------------------------------------------------
async def test_a_blocked_users_posts_never_appear(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    viewer = await make_user(db_session, at=NYERI_TOWN)
    pest = await make_user(db_session, at=offset_km(NYERI_TOWN, north=0.5))
    await make_post(db_session, redis_client, author=pest, body="Unwanted")
    await block_user(db_session, blocker=viewer, blocked_id=pest.id)
    await db_session.flush()

    assert "Unwanted" not in await feed_bodies(await authenticate(viewer))


async def test_blocking_works_in_both_directions(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """The person blocked must not keep watching the person who blocked them."""
    blocker = await make_user(db_session, at=NYERI_TOWN)
    blocked = await make_user(db_session, at=offset_km(NYERI_TOWN, north=0.5))
    await make_post(db_session, redis_client, author=blocker, body="Private life")
    await block_user(db_session, blocker=blocker, blocked_id=blocked.id)
    await db_session.flush()

    assert "Private life" not in await feed_bodies(await authenticate(blocked))


async def test_a_blocked_users_post_is_not_found_by_id(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """404, not 403: a distinct answer would confirm the block."""
    viewer = await make_user(db_session, at=NYERI_TOWN)
    pest = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=pest)
    await block_user(db_session, blocker=viewer, blocked_id=pest.id)
    await db_session.flush()

    response = await (await authenticate(viewer)).get(f"/v1/posts/{post.id}")

    assert response.status_code == 404


async def test_a_blocked_user_cannot_comment_or_react(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    pest = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    await block_user(db_session, blocker=author, blocked_id=pest.id)
    await db_session.flush()
    client = await authenticate(pest)

    comment = await client.post(f"/v1/posts/{post.id}/comments", json={"body": "hi"})
    reaction = await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})

    assert comment.status_code == 404
    assert reaction.status_code == 404


# ---------------------------------------------------------------------------
# "Uploaded images come back without EXIF"
# ---------------------------------------------------------------------------
async def test_images_on_a_post_come_back_without_exif(
    db_session: AsyncSession,
    redis_client: Redis,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    """End to end: a GPS-tagged photo goes in, and every byte that can be served
    for it is checked."""
    author = await make_user(db_session, at=NYERI_TOWN)
    asset = await make_ready_image(db_session, storage, owner=author)
    client = await authenticate(author)

    created = await client.post(
        "/v1/posts",
        json={"body": "Sunset over Kirinyaga", "category": "general", "media_ids": [str(asset.id)]},
    )
    assert created.status_code == 201, created.text

    images = created.json()["post"]["images"]
    assert len(images) == 1
    for size, variant in images[0]["variants"].items():
        key = variant["url"].removeprefix("memory://object/")
        data, _ = storage.objects[key]
        assert read_gps_tags(data) == {}, size
        assert not has_exif(data), size


async def test_the_exif_bearing_original_is_deleted(
    db_session: AsyncSession, storage: MemoryStorageProvider
) -> None:
    """Only processed versions may ever be served — so the original, the one copy
    that still has the GPS in it, must not exist at all."""
    author = await make_user(db_session, at=NYERI_TOWN)
    asset = await make_ready_image(db_session, storage, owner=author)

    assert asset.original_key is None
    for data, _ in storage.objects.values():
        assert read_gps_tags(data) == {}


# ---------------------------------------------------------------------------
# Creating posts
# ---------------------------------------------------------------------------
async def test_creating_a_post_returns_a_reach_estimate(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=40, rings=6)
    await redis_client.flushdb()

    response = await (await authenticate(author)).post(
        "/v1/posts", json={"body": "Hello Nyeri", "category": "general"}
    )

    assert response.status_code == 201
    reach = response.json()["reach_estimate"]
    assert reach > 0
    assert reach % 10 == 0  # rounded, so it is an estimate rather than a headcount


async def test_posting_requires_a_home_area(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    nowhere = await make_user(db_session)

    response = await (await authenticate(nowhere)).post(
        "/v1/posts", json={"body": "Hello", "category": "general"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["action"] == "PUT /v1/me/location"


async def test_an_unknown_category_is_refused(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)

    response = await (await authenticate(author)).post(
        "/v1/posts", json={"body": "Hello", "category": "astrology"}
    )

    assert response.status_code == 400
    assert "general" in response.json()["error"]["details"]["categories"]


@pytest.mark.parametrize(
    "category", ["general", "question", "recommendation", "lost_and_found", "alert"]
)
async def test_every_configured_category_is_accepted(
    db_session: AsyncSession, authenticate: Authenticate, category: str
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)

    response = await (await authenticate(author)).post(
        "/v1/posts", json={"body": "Hello", "category": category}
    )

    assert response.status_code == 201


async def test_an_over_long_post_is_refused(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)

    response = await (await authenticate(author)).post(
        "/v1/posts",
        json={"body": "x" * (settings.post_max_body_length + 1), "category": "general"},
    )

    assert response.status_code == 400


async def test_at_most_six_images(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    assets = [await make_ready_image(db_session, storage, owner=author) for _ in range(7)]

    response = await (await authenticate(author)).post(
        "/v1/posts",
        json={"body": "Too many", "category": "general", "media_ids": [str(a.id) for a in assets]},
    )

    assert response.status_code == 400


async def test_someone_elses_image_cannot_be_attached(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    """Otherwise a private upload could be made public on somebody else's post."""
    owner = await make_user(db_session, at=NYERI_TOWN)
    thief = await make_user(db_session, at=NYERI_TOWN)
    asset = await make_ready_image(db_session, storage, owner=owner)

    response = await (await authenticate(thief)).post(
        "/v1/posts",
        json={"body": "Mine now", "category": "general", "media_ids": [str(asset.id)]},
    )

    assert response.status_code == 403


async def test_an_avatar_upload_cannot_be_used_on_a_post(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    """The processing profile differs, so the purpose must match."""
    from app.modules.media.models import MediaPurpose

    author = await make_user(db_session, at=NYERI_TOWN)
    avatar = await make_ready_image(db_session, storage, owner=author, purpose=MediaPurpose.avatar)

    response = await (await authenticate(author)).post(
        "/v1/posts",
        json={"body": "Hi", "category": "general", "media_ids": [str(avatar.id)]},
    )

    assert response.status_code == 400


async def test_a_suspended_account_cannot_post(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    from app.modules.users.models import UserStatus

    suspended = await make_user(db_session, at=NYERI_TOWN, status=UserStatus.suspended)

    response = await (await authenticate(suspended)).post(
        "/v1/posts", json={"body": "Hi", "category": "general"}
    )

    assert response.status_code == 403


async def test_posting_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/posts", json={"body": "Hi", "category": "general"})

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# New-account limits
# ---------------------------------------------------------------------------
async def test_a_new_account_has_a_lower_daily_post_limit(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    """A throwaway account is the cheapest spam vector there is."""
    newcomer = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(newcomer)

    statuses = [
        (
            await client.post("/v1/posts", json={"body": f"Post {i}", "category": "general"})
        ).status_code
        for i in range(settings.new_account_posts_per_day + 1)
    ]

    assert (
        statuses[: settings.new_account_posts_per_day] == [201] * settings.new_account_posts_per_day
    )
    assert statuses[-1] == 429


async def test_an_established_account_gets_more_room(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    veteran = await make_user(db_session, at=NYERI_TOWN, created_days_ago=30)
    client = await authenticate(veteran)

    statuses = [
        (
            await client.post("/v1/posts", json={"body": f"Post {i}", "category": "general"})
        ).status_code
        for i in range(settings.new_account_posts_per_day + 1)
    ]

    assert 429 not in statuses


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
async def test_a_retried_post_is_created_once(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """The flaky-connection case the header exists for."""
    author = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(author)
    key = str(uuid.uuid4())
    payload = {"body": "Only once please", "category": "general"}

    first = await client.post("/v1/posts", json=payload, headers={"Idempotency-Key": key})
    second = await client.post("/v1/posts", json=payload, headers={"Idempotency-Key": key})

    assert first.status_code == second.status_code == 201
    assert first.json()["post"]["id"] == second.json()["post"]["id"]
    assert second.headers.get("Idempotent-Replayed") == "true"
    count = len(
        (await db_session.execute(select(Post).where(Post.body == "Only once please"))).all()
    )
    assert count == 1


async def test_a_reused_key_with_a_different_body_is_refused(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """A client reusing keys for different actions has broken retry logic that
    would eventually lose somebody's post."""
    author = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(author)
    key = str(uuid.uuid4())

    await client.post(
        "/v1/posts", json={"body": "First", "category": "general"}, headers={"Idempotency-Key": key}
    )
    second = await client.post(
        "/v1/posts",
        json={"body": "Different", "category": "general"},
        headers={"Idempotency-Key": key},
    )

    assert second.status_code == 422


async def test_without_a_key_each_request_creates_a_post(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(author)

    await client.post("/v1/posts", json={"body": "Twice", "category": "general"})
    await client.post("/v1/posts", json={"body": "Twice", "category": "general"})

    count = len((await db_session.execute(select(Post).where(Post.body == "Twice"))).all())
    assert count == 2


async def test_a_failed_request_frees_its_key_for_a_retry(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """Otherwise one transient failure would leave a key permanently stuck "in
    progress" and the client could never complete the action."""
    author = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(author)
    key = str(uuid.uuid4())

    failed = await client.post(
        "/v1/posts", json={"body": "x", "category": "nonsense"}, headers={"Idempotency-Key": key}
    )
    assert failed.status_code == 400

    # Same key, same body: still refused for the same reason, not "in progress".
    retried = await client.post(
        "/v1/posts", json={"body": "x", "category": "nonsense"}, headers={"Idempotency-Key": key}
    )
    assert retried.status_code == 400


# ---------------------------------------------------------------------------
# Reading, deleting, moderation state
# ---------------------------------------------------------------------------
async def test_a_post_shows_an_area_label_and_distance_band_only(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=offset_km(NYERI_TOWN, north=2.0))
    viewer = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    body = (await (await authenticate(viewer)).get(f"/v1/posts/{post.id}")).json()

    assert set(body["area_label"]) == {"label", "precision"}
    assert body["distance_bucket"] == "1_3km"
    rendered = str(body)
    for forbidden in ("latitude", "longitude", "centroid", "cell_res", str(post.cell_res9)):
        assert forbidden not in rendered


async def test_the_author_sees_no_distance_to_their_own_post(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    body = (await (await authenticate(author)).get(f"/v1/posts/{post.id}")).json()

    assert body["distance_bucket"] is None


async def test_the_author_can_delete_their_post(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Regret")
    client = await authenticate(author)

    assert (await client.delete(f"/v1/posts/{post.id}")).status_code == 204
    assert "Regret" not in await feed_bodies(client)
    assert (await client.get(f"/v1/posts/{post.id}")).status_code == 404


async def test_a_deleted_post_is_soft_deleted(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Kept for moderation evidence until the retention job runs."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    await (await authenticate(author)).delete(f"/v1/posts/{post.id}")
    await db_session.refresh(post)

    assert post.deleted_at is not None


async def test_nobody_else_can_delete_a_post(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    other = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    response = await (await authenticate(other)).delete(f"/v1/posts/{post.id}")

    assert response.status_code == 403


async def test_hidden_content_is_visible_only_to_its_author(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Being hidden without being told is worse than being told."""
    author = await make_user(db_session, at=NYERI_TOWN)
    neighbour = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Contested")
    post.status = ContentStatus.hidden_pending_review
    await db_session.flush()

    author_view = await (await authenticate(author)).get(f"/v1/posts/{post.id}")
    neighbour_view = await (await authenticate(neighbour)).get(f"/v1/posts/{post.id}")

    assert author_view.status_code == 200
    assert author_view.json()["status"] == "hidden_pending_review"
    assert neighbour_view.status_code == 404
    assert "Contested" not in await feed_bodies(await authenticate(neighbour))


async def test_removed_content_is_visible_to_nobody(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    post.status = ContentStatus.removed
    await db_session.flush()

    assert (await (await authenticate(author)).get(f"/v1/posts/{post.id}")).status_code == 404


async def test_the_feed_requires_a_home_area(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    nowhere = await make_user(db_session)

    response = await (await authenticate(nowhere)).get("/v1/feed")

    assert response.status_code == 409


async def test_the_feed_reports_its_radius(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    viewer = await make_user(db_session, at=NYERI_TOWN)

    body = (await (await authenticate(viewer)).get("/v1/feed")).json()

    assert body["radius_km"] == settings.feed_radius_max_km
    assert body["sort"] == "recent"


async def test_the_feed_requires_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/feed")).status_code == 401
