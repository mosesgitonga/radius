"""Editing a post (decision 2.22)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.moderation import console
from app.modules.moderation.models import ContentStatus, ReportTargetType
from app.modules.users.models import User
from tests.factories import NYERI_TOWN, make_post, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def test_the_author_edits_text_and_category(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Water off on Saturdy")

    response = await (await authenticate(author)).patch(
        f"/v1/posts/{post.id}", json={"body": "Water off on Saturday", "category": "alert"}
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["body"], body["category"]) == ("Water off on Saturday", "alert")
    assert body["edited_at"] is not None


async def test_only_what_is_sent_changes(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Lost: a brown dog")

    response = await (await authenticate(author)).patch(
        f"/v1/posts/{post.id}", json={"category": "lost_and_found"}
    )

    assert response.json()["body"] == "Lost: a brown dog"
    assert response.json()["category"] == "lost_and_found"


async def test_sending_the_same_text_is_not_an_edit(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Same words")

    response = await (await authenticate(author)).patch(
        f"/v1/posts/{post.id}", json={"body": "  Same words  "}
    )

    assert response.json()["edited_at"] is None
    view = await console.view_target(db_session, ReportTargetType.post, post.id)
    assert view.details["earlier_versions"] == []


async def test_nobody_else_edits_a_post(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    neighbour = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    response = await (await authenticate(neighbour)).patch(
        f"/v1/posts/{post.id}", json={"body": "Hijacked"}
    )

    assert response.status_code == 403


async def test_an_edit_is_checked_like_a_new_post(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(author)

    blank = await client.patch(f"/v1/posts/{post.id}", json={"body": "   "})
    unknown = await client.patch(f"/v1/posts/{post.id}", json={"category": "gossip"})

    assert blank.status_code == 400
    assert unknown.status_code == 400


async def test_moderators_see_what_it_said_before(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """An edit can't hide what was reported."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Something nasty")
    client = await authenticate(author)
    await client.patch(f"/v1/posts/{post.id}", json={"body": "Something nice"})
    await client.patch(
        f"/v1/posts/{post.id}", json={"body": "Something nicer", "category": "alert"}
    )

    view = await console.view_target(db_session, ReportTargetType.post, post.id)

    assert view.text == "Something nicer"
    assert [(v["body"], v["category"]) for v in view.details["earlier_versions"]] == [
        ("Something nasty", "general"),
        ("Something nice", "general"),
    ]


async def test_readers_never_see_earlier_versions(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reader = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="First draft")
    await (await authenticate(author)).patch(f"/v1/posts/{post.id}", json={"body": "Final"})

    seen = (await (await authenticate(reader)).get(f"/v1/posts/{post.id}")).text

    assert "First draft" not in seen
    assert "earlier_versions" not in seen


async def test_a_restricted_author_s_edit_is_held(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Editing must not be a way round the hold on a restricted account."""
    from datetime import UTC, datetime, timedelta

    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Fine")
    author.restricted_until = datetime.now(UTC) + timedelta(days=3)
    await db_session.flush()

    await (await authenticate(author)).patch(f"/v1/posts/{post.id}", json={"body": "Not fine"})

    await db_session.refresh(post)
    assert post.status == ContentStatus.hidden_pending_review


async def test_a_deleted_post_cannot_be_edited(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(author)
    await client.delete(f"/v1/posts/{post.id}")

    response = await client.patch(f"/v1/posts/{post.id}", json={"body": "Back again"})

    assert response.status_code == 404


async def test_the_export_includes_earlier_versions(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Before")
    client = await authenticate(author)
    await client.patch(f"/v1/posts/{post.id}", json={"body": "After"})

    export = (await client.get("/v1/me/export")).json()

    (exported,) = export["content"]["posts"]
    assert exported["body"] == "After"
    assert exported["edited_at"] is not None
    assert [v["body"] for v in exported["earlier_versions"]] == ["Before"]


async def _approval_group(
    db_session: AsyncSession, authenticate: Authenticate
) -> tuple[User, User, Any]:
    owner = await make_user(db_session, at=NYERI_TOWN)
    member = await make_user(db_session, at=NYERI_TOWN)
    group = (
        await (await authenticate(owner)).post(
            "/v1/groups",
            json={
                "name": "Kamakwa Residents",
                "category": "estate",
                "visibility": "public",
                "requires_post_approval": True,
            },
        )
    ).json()
    joined = await (await authenticate(member)).post(f"/v1/groups/{group['id']}/join")
    assert joined.status_code == 200, joined.text
    return owner, member, group


async def test_a_member_s_edit_goes_back_for_approval(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner, member, group = await _approval_group(db_session, authenticate)
    post = (
        await (await authenticate(member)).post(
            f"/v1/groups/{group['id']}/posts", json={"body": "Meeting at 6", "category": "general"}
        )
    ).json()
    await (await authenticate(owner)).post(
        f"/v1/groups/{group['id']}/posts/{post['id']}/review", json={"action": "approve"}
    )

    edited = await (await authenticate(member)).patch(
        f"/v1/posts/{post['id']}", json={"body": "Meeting at 6, bring chairs"}
    )

    assert edited.json()["approval_status"] == "pending"


async def test_a_moderator_s_edit_stays_live(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner, _member, group = await _approval_group(db_session, authenticate)
    client = await authenticate(owner)
    post = (
        await client.post(
            f"/v1/groups/{group['id']}/posts", json={"body": "Rules updated", "category": "general"}
        )
    ).json()

    edited = await client.patch(f"/v1/posts/{post['id']}", json={"body": "Rules updated: no ads"})

    assert edited.json()["approval_status"] in (None, "approved")
