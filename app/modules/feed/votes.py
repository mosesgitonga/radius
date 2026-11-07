"""Up and down votes on comments (decision 2.21).

A vote is +1 or -1, one per person per comment; voting the other way changes it,
and removing it takes it back. `comments.score` holds the net, changed by SQL
increments so two people voting at once cannot lose a vote. Top-level comments are
ranked by it.

Nobody votes on their own comment. An upvote notifies the author, collapsed like
a reaction; a downvote never notifies anyone.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError
from app.modules.feed.models import Comment, CommentVote
from app.modules.feed.service import _visible_comment, get_post, require_can_engage
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users.models import User


async def _votable(session: AsyncSession, *, viewer: User, comment_id: uuid.UUID) -> Comment:
    """A comment the viewer may vote on: one they can see and take part in, and
    not their own."""
    comment = await _visible_comment(session, viewer=viewer, comment_id=comment_id)
    post = await get_post(session, viewer=viewer, post_id=comment.post_id)
    await require_can_engage(session, post=post, viewer=viewer)
    if comment.author_id == viewer.id:
        raise BadRequestError("You can't vote on your own comment")
    return comment


async def _shift(session: AsyncSession, comment_id: uuid.UUID, by: int) -> None:
    if by:
        await session.execute(
            update(Comment).where(Comment.id == comment_id).values(score=Comment.score + by)
        )


async def _score(session: AsyncSession, comment_id: uuid.UUID) -> int:
    return int(await session.scalar(select(Comment.score).where(Comment.id == comment_id)) or 0)


async def set_vote(
    session: AsyncSession,
    *,
    user: User,
    comment_id: uuid.UUID,
    value: int,
    settings: Settings,
) -> int:
    """Vote +1 or -1, replacing any earlier vote. Returns the new score."""
    if value not in (1, -1):
        raise BadRequestError("A vote is 1 (up) or -1 (down)")
    comment = await _votable(session, viewer=user, comment_id=comment_id)

    existing = await session.scalar(
        select(CommentVote).where(
            CommentVote.user_id == user.id, CommentVote.comment_id == comment.id
        )
    )
    if existing is not None:
        if existing.value != value:
            await _shift(session, comment.id, value - existing.value)
            existing.value = value
            await session.flush()
        return await _score(session, comment.id)

    try:
        async with session.begin_nested():
            session.add(CommentVote(user_id=user.id, comment_id=comment.id, value=value))
    except IntegrityError:
        # Two taps at once: the other one won. Make it this vote.
        return await set_vote(
            session, user=user, comment_id=comment_id, value=value, settings=settings
        )
    await _shift(session, comment.id, value)

    if value == 1:
        await notifications_service.notify(
            session,
            recipient_id=comment.author_id,
            kind=NotificationType.reaction,
            actor_id=user.id,
            target_type="comment",
            target_id=comment.id,
            collapse_key=f"reaction:comment:{comment.id}",
            data={"comment_id": str(comment.id), "post_id": str(comment.post_id)},
            settings=settings,
        )
    return await _score(session, comment.id)


async def remove_vote(session: AsyncSession, *, user: User, comment_id: uuid.UUID) -> int:
    """Take a vote back. Harmless if there was none. Returns the new score."""
    comment = await _votable(session, viewer=user, comment_id=comment_id)
    removed = await session.scalar(
        delete(CommentVote)
        .where(CommentVote.user_id == user.id, CommentVote.comment_id == comment.id)
        .returning(CommentVote.value)
    )
    if removed is not None:
        await _shift(session, comment.id, -int(removed))
    return await _score(session, comment.id)


async def votes_by(
    session: AsyncSession, *, user_id: uuid.UUID, comment_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """The user's vote on each of these comments, for a page of comments."""
    if not comment_ids:
        return {}
    rows = await session.execute(
        select(CommentVote.comment_id, CommentVote.value).where(
            CommentVote.user_id == user_id, CommentVote.comment_id.in_(comment_ids)
        )
    )
    return {comment_id: int(value) for comment_id, value in rows.all()}


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def purge_user_votes(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """The person's votes go, and come back out of the scores they counted in."""
    totals = (
        await session.execute(
            select(CommentVote.comment_id, func.sum(CommentVote.value))
            .where(CommentVote.user_id == user_id)
            .group_by(CommentVote.comment_id)
        )
    ).all()
    for comment_id, total in totals:
        await _shift(session, comment_id, -int(total))
    return await execute_dml(session, delete(CommentVote).where(CommentVote.user_id == user_id))


async def export_user_votes(session: AsyncSession, *, user_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(CommentVote.comment_id, CommentVote.value, CommentVote.created_at)
        .where(CommentVote.user_id == user_id)
        .order_by(CommentVote.created_at)
    )
    return [
        {
            "comment_id": str(comment_id),
            "vote": "up" if value == 1 else "down",
            "voted_at": created_at.isoformat(),
        }
        for comment_id, value, created_at in rows.all()
    ]
