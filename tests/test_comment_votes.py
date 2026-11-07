"""Up and down votes on comments, and ranking by them (decision 2.21)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.storage import MemoryStorageProvider
from app.modules.feed.models import Comment, CommentVote
from app.modules.notifications.models import Notification
from app.modules.users.models import User
from app.modules.users.service import block_user
from tests.factories import NYERI_TOWN, make_post, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def _comment(client: httpx.AsyncClient, post_id: Any, body: str, **extra: Any) -> Any:
    response = await client.post(f"/v1/posts/{post_id}/comments", json={"body": body, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def _setup(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> tuple[Any, User, User]:
    """A post, a comment by its writer, and a neighbour to vote. The test client is
    shared, so each step signs in as whoever acts."""
    writer = await make_user(db_session, at=NYERI_TOWN)
    voter = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=writer)
    comment = await _comment(await authenticate(writer), post.id, "Try the market on Saturday")
    return comment, writer, voter


async def test_an_upvote_raises_the_score(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, writer, voter = await _setup(db_session, redis_client, authenticate)

    response = await (await authenticate(voter)).put(
        f"/v1/comments/{comment['id']}/vote", json={"value": 1}
    )

    assert response.status_code == 200
    assert response.json() == {"score": 1, "my_vote": 1}
    page = (
        await (await authenticate(voter)).get(f"/v1/posts/{comment['post_id']}/comments")
    ).json()
    assert (page["items"][0]["score"], page["items"][0]["my_vote"]) == (1, 1)
    # The writer sees the score, and no vote of their own.
    page = (
        await (await authenticate(writer)).get(f"/v1/posts/{comment['post_id']}/comments")
    ).json()
    assert (page["items"][0]["score"], page["items"][0]["my_vote"]) == (1, 0)


async def test_voting_again_the_same_way_counts_once(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, _, voter = await _setup(db_session, redis_client, authenticate)

    await (await authenticate(voter)).put(f"/v1/comments/{comment['id']}/vote", json={"value": 1})
    response = await (await authenticate(voter)).put(
        f"/v1/comments/{comment['id']}/vote", json={"value": 1}
    )

    assert response.json()["score"] == 1


async def test_switching_from_up_to_down_moves_the_score_by_two(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, _, voter = await _setup(db_session, redis_client, authenticate)

    await (await authenticate(voter)).put(f"/v1/comments/{comment['id']}/vote", json={"value": 1})
    response = await (await authenticate(voter)).put(
        f"/v1/comments/{comment['id']}/vote", json={"value": -1}
    )

    assert response.json() == {"score": -1, "my_vote": -1}


async def test_taking_a_vote_back(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, _, voter = await _setup(db_session, redis_client, authenticate)
    await (await authenticate(voter)).put(f"/v1/comments/{comment['id']}/vote", json={"value": -1})

    response = await (await authenticate(voter)).delete(f"/v1/comments/{comment['id']}/vote")
    again = await (await authenticate(voter)).delete(f"/v1/comments/{comment['id']}/vote")

    assert response.json() == {"score": 0, "my_vote": 0}
    assert again.status_code == 200
    assert again.json()["score"] == 0


async def test_a_vote_must_be_up_or_down(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, _, voter = await _setup(db_session, redis_client, authenticate)

    response = await (await authenticate(voter)).put(
        f"/v1/comments/{comment['id']}/vote", json={"value": 5}
    )

    assert response.status_code == 422


async def test_nobody_votes_on_their_own_comment(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, writer, _ = await _setup(db_session, redis_client, authenticate)

    response = await (await authenticate(writer)).put(
        f"/v1/comments/{comment['id']}/vote", json={"value": 1}
    )

    assert response.status_code == 400


async def test_a_blocked_person_cannot_vote(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Blocks hide the comment entirely, so it reads as not found."""
    comment, writer, voter = await _setup(db_session, redis_client, authenticate)
    await block_user(db_session, blocker=writer, blocked_id=voter.id)
    await db_session.flush()

    response = await (await authenticate(voter)).put(
        f"/v1/comments/{comment['id']}/vote", json={"value": 1}
    )

    assert response.status_code == 404


async def test_many_neighbours_voting_count_correctly(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, *_ = await _setup(db_session, redis_client, authenticate)
    for value in (1, 1, 1, -1):
        neighbour = await make_user(db_session, at=NYERI_TOWN)
        await (await authenticate(neighbour)).put(
            f"/v1/comments/{comment['id']}/vote", json={"value": value}
        )

    stored = await db_session.get(Comment, comment["id"])
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.score == 2


async def test_an_upvote_notifies_and_a_downvote_does_not(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, writer, *_ = await _setup(db_session, redis_client, authenticate)
    critic = await make_user(db_session, at=NYERI_TOWN)
    fan = await make_user(db_session, at=NYERI_TOWN)

    await (await authenticate(critic)).put(f"/v1/comments/{comment['id']}/vote", json={"value": -1})
    before = (
        await db_session.execute(select(Notification).where(Notification.user_id == writer.id))
    ).all()
    assert before == []

    await (await authenticate(fan)).put(f"/v1/comments/{comment['id']}/vote", json={"value": 1})
    notes = list(
        (
            await db_session.execute(select(Notification).where(Notification.user_id == writer.id))
        ).scalars()
    )
    assert len(notes) == 1
    assert notes[0].actor_id == fan.id


async def test_top_comments_come_highest_score_first_then_oldest(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    writer = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=writer)
    client = await authenticate(writer)
    first = await _comment(client, post.id, "first")
    second = await _comment(client, post.id, "second")
    third = await _comment(client, post.id, "third")
    fourth = await _comment(client, post.id, "fourth")
    for comment, votes in ((third, (1, 1)), (second, (1,)), (fourth, (1,)), (first, (-1,))):
        for value in votes:
            neighbour = await make_user(db_session, at=NYERI_TOWN)
            await (await authenticate(neighbour)).put(
                f"/v1/comments/{comment['id']}/vote", json={"value": value}
            )

    page = (await client.get(f"/v1/posts/{post.id}/comments")).json()

    assert [item["body"] for item in page["items"]] == ["third", "second", "fourth", "first"]


async def test_ranked_comments_page_without_gaps_or_repeats(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    writer = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=writer)
    client = await authenticate(writer)
    bodies = [f"c{index}" for index in range(7)]
    made = [await _comment(client, post.id, body) for body in bodies]
    fan = await authenticate(await make_user(db_session, at=NYERI_TOWN))
    for comment in (made[5], made[2]):
        await fan.put(f"/v1/comments/{comment['id']}/vote", json={"value": 1})

    seen: list[str] = []
    cursor: str | None = None
    while True:
        query = f"?limit=2&cursor={cursor}" if cursor else "?limit=2"
        page = (await client.get(f"/v1/posts/{post.id}/comments{query}")).json()
        seen += [item["body"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == ["c2", "c5", "c0", "c1", "c3", "c4", "c6"]


async def test_a_mangled_cursor_is_refused(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, writer, _ = await _setup(db_session, redis_client, authenticate)

    response = await (await authenticate(writer)).get(
        f"/v1/posts/{comment['post_id']}/comments?cursor=not-a-cursor"
    )

    assert response.status_code == 400


async def test_replies_stay_oldest_first_and_can_be_voted_on(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, writer, voter = await _setup(db_session, redis_client, authenticate)
    early = await _comment(
        await authenticate(voter), comment["post_id"], "early", parent_id=comment["id"]
    )
    late = await _comment(
        await authenticate(voter), comment["post_id"], "late", parent_id=comment["id"]
    )

    vote = await (await authenticate(writer)).put(
        f"/v1/comments/{late['id']}/vote", json={"value": 1}
    )
    replies = (
        await (await authenticate(writer)).get(
            f"/v1/posts/{comment['post_id']}/comments?parent_id={comment['id']}"
        )
    ).json()

    assert vote.json()["score"] == 1
    assert [item["body"] for item in replies["items"]] == ["early", "late"]
    assert replies["items"][0]["id"] == early["id"]


async def test_purging_an_account_takes_its_votes_out_of_the_scores(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    storage: MemoryStorageProvider,
) -> None:
    from app.core.config import get_settings
    from app.modules.users.tasks import purge_deleted_accounts

    comment, _, voter = await _setup(db_session, redis_client, authenticate)
    await (await authenticate(voter)).put(f"/v1/comments/{comment['id']}/vote", json={"value": 1})
    voter.deletion_requested_at = datetime.now(UTC) - timedelta(days=60)
    await db_session.flush()

    await purge_deleted_accounts(db_session, settings=get_settings(), storage=storage)

    stored = await db_session.get(Comment, comment["id"])
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.score == 0
    left = (
        await db_session.execute(select(CommentVote).where(CommentVote.user_id == voter.id))
    ).all()
    assert left == []


async def test_the_export_includes_votes(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    comment, _, voter = await _setup(db_session, redis_client, authenticate)
    await (await authenticate(voter)).put(f"/v1/comments/{comment['id']}/vote", json={"value": -1})

    export = (await (await authenticate(voter)).get("/v1/me/export")).json()

    assert [(v["comment_id"], v["vote"]) for v in export["comment_votes"]] == [
        (comment["id"], "down")
    ]
