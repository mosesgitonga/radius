"""Comments, reactions, and stories."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.storage import MemoryStorageProvider
from app.modules.feed.models import Comment, Post, Story
from app.modules.media.models import MediaPurpose
from app.modules.users.models import User
from app.modules.users.service import block_user
from app.workers.tasks import expire_old_stories
from tests.factories import (
    KARATINA,
    NAIROBI,
    NYERI_TOWN,
    make_post,
    make_ready_image,
    make_user,
    offset_km,
)

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
async def test_commenting_on_a_post(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    neighbour = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    response = await (await authenticate(neighbour)).post(
        f"/v1/posts/{post.id}/comments", json={"body": "I saw it near the church"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["body"] == "I saw it near the church"
    assert body["is_visiting"] is False
    await db_session.refresh(post)
    assert post.comment_count == 1


async def test_replying_to_a_comment(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(author)
    parent = (await client.post(f"/v1/posts/{post.id}/comments", json={"body": "Top"})).json()

    reply = await client.post(
        f"/v1/posts/{post.id}/comments", json={"body": "Reply", "parent_id": parent["id"]}
    )

    assert reply.status_code == 201
    assert reply.json()["parent_id"] == parent["id"]
    replies = (await client.get(f"/v1/posts/{post.id}/comments?parent_id={parent['id']}")).json()
    assert [item["body"] for item in replies["items"]] == ["Reply"]


async def test_replies_to_replies_are_refused(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """One level, as the brief specifies."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(author)
    top = (await client.post(f"/v1/posts/{post.id}/comments", json={"body": "Top"})).json()
    reply = (
        await client.post(
            f"/v1/posts/{post.id}/comments", json={"body": "Reply", "parent_id": top["id"]}
        )
    ).json()

    response = await client.post(
        f"/v1/posts/{post.id}/comments", json={"body": "Deeper", "parent_id": reply["id"]}
    )

    assert response.status_code == 400


async def test_comments_read_oldest_first(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """A conversation reads top to bottom, unlike the feed."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(author)
    for body in ("first", "second", "third"):
        await client.post(f"/v1/posts/{post.id}/comments", json={"body": body})

    page = (await client.get(f"/v1/posts/{post.id}/comments")).json()

    assert [item["body"] for item in page["items"]] == ["first", "second", "third"]


async def test_a_visitors_comment_is_marked_and_shows_their_home(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Decision 1.1, rule 3: locals can tell a visitor from a neighbour, and the
    visitor is labelled with where they live, not where they are standing."""
    local = await make_user(db_session, at=KARATINA)
    visitor = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=local)
    client = await authenticate(visitor)
    await client.post("/v1/me/explore", json=KARATINA.as_payload())

    response = await client.post(
        f"/v1/posts/{post.id}/comments", json={"body": "Visiting for the weekend"}
    )

    assert response.status_code == 201
    assert response.json()["is_visiting"] is True


async def test_comments_from_blocked_users_are_hidden(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    pest = await make_user(db_session, at=NYERI_TOWN)
    reader = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    await (await authenticate(pest)).post(
        f"/v1/posts/{post.id}/comments", json={"body": "Rude remark"}
    )
    await block_user(db_session, blocker=reader, blocked_id=pest.id)
    await db_session.flush()

    page = (await (await authenticate(reader)).get(f"/v1/posts/{post.id}/comments")).json()

    assert "Rude remark" not in [item["body"] for item in page["items"]]


async def test_the_post_author_can_delete_a_comment_on_their_post(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """So abuse can be cleared from under a lost-dog post without waiting for a
    moderator."""
    author = await make_user(db_session, at=NYERI_TOWN)
    troll = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    comment = (
        await (await authenticate(troll)).post(
            f"/v1/posts/{post.id}/comments", json={"body": "Abuse"}
        )
    ).json()

    response = await (await authenticate(author)).delete(f"/v1/comments/{comment['id']}")

    assert response.status_code == 204
    await db_session.refresh(post)
    assert post.comment_count == 0


async def test_a_bystander_cannot_delete_a_comment(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    commenter = await make_user(db_session, at=NYERI_TOWN)
    bystander = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    comment = (
        await (await authenticate(commenter)).post(
            f"/v1/posts/{post.id}/comments", json={"body": "Hello"}
        )
    ).json()

    response = await (await authenticate(bystander)).delete(f"/v1/comments/{comment['id']}")

    assert response.status_code == 403


async def test_an_empty_comment_is_refused(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    response = await (await authenticate(author)).post(
        f"/v1/posts/{post.id}/comments", json={"body": "   "}
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------
async def test_reacting_counts_once_per_person(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    fan = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(fan)

    first = await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})
    second = await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})

    assert first.json() == {"kind": "like", "reaction_count": 1}
    assert second.json() == {"kind": "like", "reaction_count": 1}


async def test_changing_a_reaction_replaces_it(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    fan = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(fan)
    await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})

    changed = await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "helpful"})

    assert changed.json() == {"kind": "helpful", "reaction_count": 1}
    feed_item = (await client.get(f"/v1/posts/{post.id}")).json()
    assert feed_item["my_reaction"] == "helpful"


async def test_removing_a_reaction(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    fan = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(fan)
    await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "love"})

    removed = await client.delete(f"/v1/posts/{post.id}/reaction")
    removed_again = await client.delete(f"/v1/posts/{post.id}/reaction")

    assert removed.json() == {"kind": None, "reaction_count": 0}
    assert removed_again.json() == {"kind": None, "reaction_count": 0}


async def test_many_neighbours_reacting_count_correctly(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """The counter is incremented in SQL, not read-modify-write, so it cannot lose
    updates."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    for _ in range(5):
        fan = await make_user(db_session, at=NYERI_TOWN)
        await (await authenticate(fan)).put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})

    await db_session.refresh(post)

    assert post.reaction_count == 5


async def test_an_unknown_reaction_is_refused(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    response = await (await authenticate(author)).put(
        f"/v1/posts/{post.id}/reaction", json={"kind": "angry_face"}
    )

    assert response.status_code == 400


async def test_reacting_to_a_comment(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(author)
    comment = (await client.post(f"/v1/posts/{post.id}/comments", json={"body": "Hi"})).json()

    response = await client.put(f"/v1/comments/{comment['id']}/reaction", json={"kind": "like"})

    assert response.json() == {"kind": "like", "reaction_count": 1}


# ---------------------------------------------------------------------------
# Stories
# ---------------------------------------------------------------------------
async def test_a_text_story(db_session: AsyncSession, authenticate: Authenticate) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)

    response = await (await authenticate(author)).post(
        "/v1/stories", json={"kind": "text", "text": "Rain on the way"}
    )

    assert response.status_code == 201
    body = response.json()
    expires = datetime.fromisoformat(body["expires_at"])
    created = datetime.fromisoformat(body["created_at"])
    assert timedelta(hours=23) < expires - created <= timedelta(hours=24, minutes=1)


async def test_an_image_story_strips_metadata(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    from app.modules.media.images import read_gps_tags

    author = await make_user(db_session, at=NYERI_TOWN)
    image = await make_ready_image(db_session, storage, owner=author, purpose=MediaPurpose.story)

    response = await (await authenticate(author)).post(
        "/v1/stories", json={"kind": "image", "media_id": str(image.id)}
    )

    assert response.status_code == 201
    for variant in response.json()["image"].values():
        data, _ = storage.objects[variant["url"].removeprefix("memory://object/")]
        assert read_gps_tags(data) == {}


async def test_a_story_kind_must_match_its_content(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(author)

    no_text = await client.post("/v1/stories", json={"kind": "text"})
    no_image = await client.post("/v1/stories", json={"kind": "image"})

    assert no_text.status_code == 400
    assert no_image.status_code == 400


async def test_stories_respect_the_radius(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    near = await make_user(db_session, at=offset_km(NYERI_TOWN, north=1.0))
    far = await make_user(db_session, at=NAIROBI)
    await (await authenticate(author)).post("/v1/stories", json={"kind": "text", "text": "Hello"})

    near_page = (await (await authenticate(near)).get("/v1/stories")).json()
    far_page = (await (await authenticate(far)).get("/v1/stories")).json()

    assert [item["text"] for item in near_page["items"]] == ["Hello"]
    assert far_page["items"] == []


async def test_viewing_records_one_view_per_person(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """The author learns who looked, not how often."""
    author = await make_user(db_session, at=NYERI_TOWN)
    viewer = await make_user(db_session, at=NYERI_TOWN)
    story = (
        await (await authenticate(author)).post("/v1/stories", json={"kind": "text", "text": "Hi"})
    ).json()
    viewer_client = await authenticate(viewer)

    for _ in range(3):
        await viewer_client.post(f"/v1/stories/{story['id']}/view")

    viewers = (await (await authenticate(author)).get(f"/v1/stories/{story['id']}/viewers")).json()
    assert [item["user"]["id"] for item in viewers["items"]] == [str(viewer.id)]
    row = (await db_session.execute(select(Story).where(Story.id == story["id"]))).scalar_one()
    assert row.view_count == 1


async def test_the_author_does_not_count_as_a_viewer(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(author)
    story = (await client.post("/v1/stories", json={"kind": "text", "text": "Hi"})).json()

    await client.post(f"/v1/stories/{story['id']}/view")

    viewers = (await client.get(f"/v1/stories/{story['id']}/viewers")).json()
    assert viewers["items"] == []


async def test_only_the_author_sees_the_viewer_list(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    other = await make_user(db_session, at=NYERI_TOWN)
    story = (
        await (await authenticate(author)).post("/v1/stories", json={"kind": "text", "text": "Hi"})
    ).json()

    response = await (await authenticate(other)).get(f"/v1/stories/{story['id']}/viewers")

    assert response.status_code == 404


async def test_the_view_count_is_shown_only_to_the_author(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    other = await make_user(db_session, at=NYERI_TOWN)
    await (await authenticate(author)).post("/v1/stories", json={"kind": "text", "text": "Hi"})

    author_page = (await (await authenticate(author)).get("/v1/stories")).json()
    other_page = (await (await authenticate(other)).get("/v1/stories")).json()

    assert author_page["items"][0]["view_count"] == 0
    assert other_page["items"][0]["view_count"] is None


async def test_an_expired_story_is_not_served_even_before_the_sweep(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """Reads filter on expiry, because the sweep is not instant."""
    author = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(author)
    story = (await client.post("/v1/stories", json={"kind": "text", "text": "Old"})).json()
    row = (await db_session.execute(select(Story).where(Story.id == story["id"]))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    assert (await client.get("/v1/stories")).json()["items"] == []


async def test_the_sweep_deletes_expired_stories_and_their_images(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    """The promise is that a story is gone in a day — deleted, images included,
    not merely hidden."""
    author = await make_user(db_session, at=NYERI_TOWN)
    image = await make_ready_image(db_session, storage, owner=author, purpose=MediaPurpose.story)
    story = (
        await (await authenticate(author)).post(
            "/v1/stories", json={"kind": "image", "media_id": str(image.id)}
        )
    ).json()
    row = (await db_session.execute(select(Story).where(Story.id == story["id"]))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    result = await expire_old_stories({"job_id": "t", "session": db_session})

    assert result["deleted"] == 1
    assert (
        await db_session.execute(select(Story).where(Story.id == story["id"]))
    ).scalar_one_or_none() is None
    assert storage.objects == {}


async def test_a_reported_story_survives_expiry_until_reviewed(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """Deleting on schedule would destroy the evidence for exactly the stories most
    likely to need it."""
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await make_user(db_session, at=NYERI_TOWN)
    story = (
        await (await authenticate(author)).post(
            "/v1/stories", json={"kind": "text", "text": "Something awful"}
        )
    ).json()
    await (await authenticate(reporter)).post(
        "/v1/reports",
        json={"target_type": "story", "target_id": story["id"], "reason": "harassment"},
    )
    row = (await db_session.execute(select(Story).where(Story.id == story["id"]))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    result = await expire_old_stories({"job_id": "t", "session": db_session})

    assert result["deleted"] == 0
    assert result["held_for_review"] == 1


# ---------------------------------------------------------------------------
# Account purge reaches content
# ---------------------------------------------------------------------------
async def test_purging_an_account_removes_its_content_and_images(
    db_session: AsyncSession,
    redis_client: Redis,
    storage: MemoryStorageProvider,
) -> None:
    """The data map's rule: a phase that adds personal data extends the purge in
    the same change. Posts, comments, stories and images all go."""
    from app.core.config import get_settings
    from app.modules.users.tasks import purge_deleted_accounts

    leaver = await make_user(db_session, at=NYERI_TOWN)
    image = await make_ready_image(db_session, storage, owner=leaver)
    post = await make_post(db_session, redis_client, author=leaver, media_ids=[image.id])
    leaver.deletion_requested_at = datetime.now(UTC) - timedelta(days=60)
    await db_session.flush()

    await purge_deleted_accounts(db_session, settings=get_settings(), storage=storage)

    await db_session.refresh(post)
    assert post.deleted_at is not None
    assert storage.objects == {}
    comments = (
        await db_session.execute(
            select(Comment).where(Comment.author_id == leaver.id, Comment.deleted_at.is_(None))
        )
    ).all()
    assert comments == []
    posts_left = (
        await db_session.execute(
            select(Post).where(Post.author_id == leaver.id, Post.deleted_at.is_(None))
        )
    ).all()
    assert posts_left == []


async def test_the_export_includes_content(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    await make_post(db_session, redis_client, author=author, body="My first post")

    export = (await (await authenticate(author)).get("/v1/me/export")).json()

    assert [p["body"] for p in export["content"]["posts"]] == ["My first post"]
    assert "media" in export
    assert any("EXIF" in item for item in export["not_held"])
