"""Feed service: posts, the feed query, comments, reactions, and stories.

Three rules from earlier phases meet here for the first time, and every read path
in this module applies all three:

1. **Radius.** Content is shown to a viewer only if it lies within their adaptive
   feed radius, measured from their home area (or explore anchor).
2. **Blocking.** Content by anyone the viewer has blocked, or who has blocked the
   viewer, is never returned — through `users.service.blocked_user_ids`, the one
   implementation of that rule.
3. **Moderation state.** `visible` content is shown to everyone; content hidden
   pending review is shown only to its author; removed content to nobody.

Authored content always takes the author's **home** location, even while they are
exploring. That is decision 1.1, rule 1, and it is what stops someone dropping posts
into a neighbourhood they do not live in.

Counters (`comment_count`, `reaction_count`, ...) are changed with SQL `UPDATE ...
SET n = n + 1` rather than by mutating a loaded object. Two neighbours reacting in
the same instant would otherwise both read 4 and both write 5.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.pagination import PageParams, paginate
from app.modules.feed.models import (
    Comment,
    Post,
    PostImage,
    Reaction,
    Story,
    StoryKind,
    StoryView,
)
from app.modules.location.radius import RadiusEngine, RadiusResult
from app.modules.location.service import (
    Anchor,
    AnchorMode,
    home_location_columns,
    is_exploring,
    resolve_anchor,
)
from app.modules.media.models import MediaPurpose
from app.modules.media.service import claim_assets_for
from app.modules.moderation.models import ContentStatus
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users.models import User
from app.modules.users.service import blocked_user_ids, is_blocked_between

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeedPage:
    posts: list[Post]
    next_cursor: str | None
    radius: RadiusResult
    anchor: Anchor


@dataclass(frozen=True, slots=True)
class StoryPage:
    stories: list[Story]
    next_cursor: str | None
    anchor: Anchor


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------
def visible_to(status: ContentStatus, *, author_id: uuid.UUID, viewer_id: uuid.UUID) -> bool:
    """Whether a piece of content's moderation state lets this viewer see it.

    Hidden content stays visible to its author. Being made invisible without being
    told is worse than being told, and the author is the one person for whom
    seeing it cannot amplify whatever got it reported.
    """
    if status == ContentStatus.visible:
        return True
    if status == ContentStatus.hidden_pending_review:
        return author_id == viewer_id
    return False


def _require_home(author: User) -> dict[str, object]:
    location = home_location_columns(author)
    if location is None:
        raise ConflictError(
            "Set your home area before posting",
            details={"action": "PUT /v1/me/location"},
        )
    return location


async def _require_anchor(redis: Redis, *, viewer: User, mode: AnchorMode) -> Anchor:
    anchor = await resolve_anchor(redis, user=viewer, mode=mode)
    if anchor is None:
        raise ConflictError(
            "Set your home area to see what is happening nearby",
            details={"action": "PUT /v1/me/location"},
        )
    return anchor


def _within(column: Any, anchor: Anchor, radius_m: int) -> Any:
    """`ST_DWithin` against the anchor's cell centre.

    The WKT is a bound parameter, not interpolated: it is built from our own cell
    arithmetic, but there is no reason to make that an assumption the SQL relies on.
    """
    return func.ST_DWithin(column, func.ST_GeogFromText(anchor.centroid_wkt), radius_m)


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------
async def create_post(
    session: AsyncSession,
    redis: Redis,
    *,
    author: User,
    body: str,
    category: str,
    media_ids: list[uuid.UUID],
    settings: Settings,
) -> tuple[Post, int]:
    """Create a post, and estimate who can see it."""
    location = _require_home(author)

    cleaned = body.strip()
    if not cleaned:
        raise BadRequestError("A post cannot be empty")
    if len(cleaned) > settings.post_max_body_length:
        raise BadRequestError(
            f"Posts are limited to {settings.post_max_body_length} characters",
            details={"max_length": settings.post_max_body_length},
        )
    if category not in settings.post_category_list:
        raise BadRequestError(
            "That category does not exist",
            details={"categories": list(settings.post_category_list)},
        )
    if len(media_ids) > settings.post_max_images:
        raise BadRequestError(
            f"A post can have at most {settings.post_max_images} images",
            details={"max_images": settings.post_max_images},
        )

    assets = await claim_assets_for(
        session, media_ids=media_ids, owner_id=author.id, purpose=MediaPurpose.post
    )

    post = Post(author_id=author.id, body=cleaned, category=category, **location)
    post.images = [
        PostImage(media_id=asset.id, position=position) for position, asset in enumerate(assets)
    ]
    session.add(post)
    await session.flush()

    reach = await estimate_reach(session, redis, cell_res8=post.cell_res8, settings=settings)
    post.reach_estimate = reach
    await session.flush()

    logger.info(
        "post_created",
        extra={"post_id": str(post.id), "images": len(assets), "category": category},
    )
    return post, reach


async def estimate_reach(
    session: AsyncSession, redis: Redis, *, cell_res8: int, settings: Settings
) -> int:
    """Roughly how many active neighbours can see a post at this location.

    The brief defines reach as the active users in every cell whose *own* radius
    covers the post. Computing that exactly means a radius per surrounding cell.
    This approximation uses the post's own cell's feed radius instead, which holds
    because radius tracks density and density varies smoothly over a few
    kilometres: cells near the post have radii near the post's. The brief allows an
    approximation, and this one costs one cached radius and one indexed read.

    Rounded to the nearest ten. "Visible to about 340 neighbours" is the message,
    and an exact figure would be a precise count of who lives nearby.
    """
    engine = RadiusEngine(session, redis, settings)
    radius = await engine.radius_for(RadiusFeature.feed, cell_res8)
    active = await engine.active_users_within(cell_res8, radius.radius_km)
    return int(round(active, -1))


async def get_post(session: AsyncSession, *, viewer: User, post_id: uuid.UUID) -> Post:
    """A post the viewer may see, or `NotFoundError`.

    404 for every refusal — blocked, hidden, removed, deleted, nonexistent. A
    distinct answer for "blocked" would let somebody confirm a block, and a
    distinct answer for "hidden" would tell a reporter their report worked.

    Radius is deliberately not checked here: a post somebody shared by link should
    open. The radius governs what is *surfaced* in the feed, not what may be read.
    """
    post = (
        await session.execute(select(Post).where(Post.id == post_id, Post.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if post is None or post.group_id is not None:
        raise NotFoundError("Post not found")
    if not visible_to(post.status, author_id=post.author_id, viewer_id=viewer.id):
        raise NotFoundError("Post not found")
    if await is_blocked_between(session, viewer.id, post.author_id):
        raise NotFoundError("Post not found")
    return post


async def delete_post(session: AsyncSession, *, viewer: User, post_id: uuid.UUID) -> None:
    """Soft-delete one's own post.

    Soft, because moderation needs the evidence if it was reported; the retention
    job purges it later.
    """
    post = await get_post(session, viewer=viewer, post_id=post_id)
    if post.author_id != viewer.id:
        raise ForbiddenError("Only the author can delete a post")
    post.deleted_at = datetime.now(UTC)
    await session.flush()


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------
async def feed_page(
    session: AsyncSession,
    redis: Redis,
    *,
    viewer: User,
    params: PageParams,
    settings: Settings,
    mode: AnchorMode = "home",
) -> FeedPage:
    """Posts within the viewer's feed radius, newest first.

    One spatial predicate on one GiST index, plus the blocking and moderation
    filters, keyset-paginated. Ranking can be added later by swapping the ordering
    and the cursor's sort key; the response shape does not change.
    """
    anchor = await _require_anchor(redis, viewer=viewer, mode=mode)
    engine = RadiusEngine(session, redis, settings)
    radius = await engine.radius_for(RadiusFeature.feed, anchor.cell_res8)
    blocked = await blocked_user_ids(session, viewer.id)

    statement: Select[Post] = select(Post).where(
        Post.status == ContentStatus.visible,
        Post.deleted_at.is_(None),
        # Group posts are scoped to their group, from Phase 4. Required to be null
        # here so they cannot leak into the public feed the moment they exist.
        Post.group_id.is_(None),
        _within(Post.centroid, anchor, radius.radius_m),
    )
    if blocked:
        statement = statement.where(Post.author_id.not_in(blocked))

    posts, next_cursor = await paginate(
        session,
        statement,
        sort_column=Post.created_at,
        id_column=Post.id,
        params=params,
    )
    return FeedPage(posts=posts, next_cursor=next_cursor, radius=radius, anchor=anchor)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
async def add_comment(
    session: AsyncSession,
    redis: Redis,
    *,
    author: User,
    post_id: uuid.UUID,
    body: str,
    parent_id: uuid.UUID | None,
    settings: Settings,
) -> Comment:
    post = await get_post(session, viewer=author, post_id=post_id)

    cleaned = body.strip()
    if not cleaned:
        raise BadRequestError("A comment cannot be empty")
    if len(cleaned) > settings.comment_max_length:
        raise BadRequestError(
            f"Comments are limited to {settings.comment_max_length} characters",
            details={"max_length": settings.comment_max_length},
        )

    parent: Comment | None = None
    if parent_id is not None:
        parent = await _visible_comment(
            session, viewer=author, comment_id=parent_id, post_id=post.id
        )
        if parent.parent_id is not None:
            # One level, as the brief specifies. Unbounded nesting is unreadable
            # on a phone and turns every comment list into a recursive query.
            raise BadRequestError(
                "You can reply to a comment, but not to a reply",
                details={"reply_to": str(parent.parent_id)},
            )

    comment = Comment(
        post_id=post.id,
        author_id=author.id,
        parent_id=parent.id if parent else None,
        body=cleaned,
        is_visiting=await is_exploring(redis, user_id=author.id),
    )
    session.add(comment)
    await session.flush()

    await session.execute(
        update(Post).where(Post.id == post.id).values(comment_count=Post.comment_count + 1)
    )
    if parent is not None:
        await session.execute(
            update(Comment)
            .where(Comment.id == parent.id)
            .values(reply_count=Comment.reply_count + 1)
        )
    await _notify_comment(session, post=post, comment=comment, parent=parent, settings=settings)
    return comment


async def _notify_comment(
    session: AsyncSession,
    *,
    post: Post,
    comment: Comment,
    parent: Comment | None,
    settings: Settings,
) -> None:
    """A reply notifies the comment's author; any comment notifies the post's.

    One notification per person: a post author replied to under their own
    comment hears about the reply, not about a comment as well.
    """
    data = {"post_id": str(post.id), "comment_id": str(comment.id)}
    if parent is not None:
        await notifications_service.notify(
            session,
            recipient_id=parent.author_id,
            kind=NotificationType.reply,
            actor_id=comment.author_id,
            target_type="comment",
            target_id=parent.id,
            collapse_key=f"reply:{parent.id}",
            data=data,
            settings=settings,
        )
    if parent is None or parent.author_id != post.author_id:
        await notifications_service.notify(
            session,
            recipient_id=post.author_id,
            kind=NotificationType.comment,
            actor_id=comment.author_id,
            target_type="post",
            target_id=post.id,
            collapse_key=f"comment:{post.id}",
            data=data,
            settings=settings,
        )


async def _visible_comment(
    session: AsyncSession,
    *,
    viewer: User,
    comment_id: uuid.UUID,
    post_id: uuid.UUID | None = None,
) -> Comment:
    comment = (
        await session.execute(
            select(Comment).where(Comment.id == comment_id, Comment.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if comment is None or (post_id is not None and comment.post_id != post_id):
        raise NotFoundError("Comment not found")
    if not visible_to(comment.status, author_id=comment.author_id, viewer_id=viewer.id):
        raise NotFoundError("Comment not found")
    if await is_blocked_between(session, viewer.id, comment.author_id):
        raise NotFoundError("Comment not found")
    return comment


async def list_comments(
    session: AsyncSession,
    *,
    viewer: User,
    post_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    params: PageParams,
) -> tuple[list[Comment], str | None]:
    """Top-level comments on a post, or the replies to one, oldest first.

    Oldest first because a conversation reads top to bottom — unlike the feed,
    where the newest thing is the most useful one.
    """
    post = await get_post(session, viewer=viewer, post_id=post_id)
    if parent_id is not None:
        await _visible_comment(session, viewer=viewer, comment_id=parent_id, post_id=post.id)

    blocked = await blocked_user_ids(session, viewer.id)
    statement: Select[Comment] = select(Comment).where(
        Comment.post_id == post.id,
        Comment.deleted_at.is_(None),
        Comment.parent_id.is_(None) if parent_id is None else Comment.parent_id == parent_id,
        # Visible to everyone, or hidden but the viewer's own.
        (Comment.status == ContentStatus.visible)
        | (
            (Comment.status == ContentStatus.hidden_pending_review)
            & (Comment.author_id == viewer.id)
        ),
    )
    if blocked:
        statement = statement.where(Comment.author_id.not_in(blocked))

    return await paginate(
        session,
        statement,
        sort_column=Comment.created_at,
        id_column=Comment.id,
        params=params,
        descending=False,
    )


async def delete_comment(session: AsyncSession, *, viewer: User, comment_id: uuid.UUID) -> None:
    """Soft-delete a comment. The comment's author, or the post's author, may.

    The post author gets the power because the thread is under their post: a
    neighbour asking about a lost dog should be able to clear abuse from beneath
    it without waiting for a moderator.
    """
    comment = await _visible_comment(session, viewer=viewer, comment_id=comment_id)
    post = (await session.execute(select(Post).where(Post.id == comment.post_id))).scalar_one()
    if viewer.id not in (comment.author_id, post.author_id):
        raise ForbiddenError("You cannot delete this comment")

    comment.deleted_at = datetime.now(UTC)
    await session.execute(
        update(Post)
        .where(Post.id == post.id, Post.comment_count > 0)
        .values(comment_count=Post.comment_count - 1)
    )
    if comment.parent_id is not None:
        await session.execute(
            update(Comment)
            .where(Comment.id == comment.parent_id, Comment.reply_count > 0)
            .values(reply_count=Comment.reply_count - 1)
        )
    await session.flush()


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------
async def set_reaction(
    session: AsyncSession,
    *,
    user: User,
    kind: str,
    settings: Settings,
    post_id: uuid.UUID | None = None,
    comment_id: uuid.UUID | None = None,
) -> tuple[str, int]:
    """React to a post or comment, replacing any earlier reaction.

    One per person per target: changing a reaction replaces it and leaves the
    count alone; a new one increments it. The insert runs inside a savepoint so
    that two taps arriving together — a common thing on a laggy connection — end
    up as one reaction rather than an integrity error.
    """
    if kind not in settings.reaction_kind_list:
        raise BadRequestError(
            "That reaction does not exist",
            details={"kinds": list(settings.reaction_kind_list)},
        )
    target_model, target_id = await _reaction_target(
        session, viewer=user, post_id=post_id, comment_id=comment_id
    )

    existing = await _find_reaction(session, user.id, post_id=post_id, comment_id=comment_id)
    if existing is not None:
        existing.kind = kind
        await session.flush()
        return kind, await _reaction_count(session, target_model, target_id)

    try:
        async with session.begin_nested():
            session.add(
                Reaction(user_id=user.id, post_id=post_id, comment_id=comment_id, kind=kind)
            )
    except IntegrityError:
        # The concurrent tap won. Ours becomes an update of theirs.
        existing = await _find_reaction(session, user.id, post_id=post_id, comment_id=comment_id)
        if existing is not None:
            existing.kind = kind
            await session.flush()
        return kind, await _reaction_count(session, target_model, target_id)

    await session.execute(
        update(target_model)
        .where(target_model.id == target_id)
        .values(reaction_count=target_model.reaction_count + 1)
    )
    # Only a new reaction notifies; changing one's reaction is not news.
    target_name = "post" if target_model is Post else "comment"
    target_author = await session.scalar(
        select(target_model.author_id).where(target_model.id == target_id)
    )
    if target_author is not None:
        await notifications_service.notify(
            session,
            recipient_id=target_author,
            kind=NotificationType.reaction,
            actor_id=user.id,
            target_type=target_name,
            target_id=target_id,
            collapse_key=f"reaction:{target_name}:{target_id}",
            data={f"{target_name}_id": str(target_id)},
            settings=settings,
        )
    return kind, await _reaction_count(session, target_model, target_id)


async def remove_reaction(
    session: AsyncSession,
    *,
    user: User,
    post_id: uuid.UUID | None = None,
    comment_id: uuid.UUID | None = None,
) -> int:
    """Remove the caller's reaction. Succeeds if there was none, since the
    outcome the client wants already holds."""
    target_model, target_id = await _reaction_target(
        session, viewer=user, post_id=post_id, comment_id=comment_id
    )
    existing = await _find_reaction(session, user.id, post_id=post_id, comment_id=comment_id)
    if existing is not None:
        await session.delete(existing)
        await session.flush()
        await session.execute(
            update(target_model)
            .where(target_model.id == target_id, target_model.reaction_count > 0)
            .values(reaction_count=target_model.reaction_count - 1)
        )
    return await _reaction_count(session, target_model, target_id)


async def _reaction_target(
    session: AsyncSession,
    *,
    viewer: User,
    post_id: uuid.UUID | None,
    comment_id: uuid.UUID | None,
) -> tuple[Any, uuid.UUID]:
    if (post_id is None) == (comment_id is None):  # pragma: no cover — router guarantees
        raise BadRequestError("React to exactly one post or comment")
    if post_id is not None:
        post = await get_post(session, viewer=viewer, post_id=post_id)
        return Post, post.id
    assert comment_id is not None  # noqa: S101 — narrowed above
    comment = await _visible_comment(session, viewer=viewer, comment_id=comment_id)
    # Reacting to a comment also requires being able to see the post it is on.
    await get_post(session, viewer=viewer, post_id=comment.post_id)
    return Comment, comment.id


async def _find_reaction(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    post_id: uuid.UUID | None,
    comment_id: uuid.UUID | None,
) -> Reaction | None:
    statement = select(Reaction).where(Reaction.user_id == user_id)
    statement = (
        statement.where(Reaction.post_id == post_id)
        if post_id is not None
        else statement.where(Reaction.comment_id == comment_id)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def _reaction_count(session: AsyncSession, model: Any, target_id: uuid.UUID) -> int:
    count = await session.scalar(select(model.reaction_count).where(model.id == target_id))
    return int(count or 0)


async def reactions_by(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    post_ids: list[uuid.UUID] | None = None,
    comment_ids: list[uuid.UUID] | None = None,
) -> dict[uuid.UUID, str]:
    """The viewer's own reactions across a page, in one query.

    Without this a feed page would need one lookup per post to fill in
    `my_reaction`.
    """
    statement = select(Reaction).where(Reaction.user_id == user_id)
    if post_ids:
        statement = statement.where(Reaction.post_id.in_(post_ids))
        rows = (await session.execute(statement)).scalars().all()
        return {row.post_id: row.kind for row in rows if row.post_id is not None}
    if comment_ids:
        statement = statement.where(Reaction.comment_id.in_(comment_ids))
        rows = (await session.execute(statement)).scalars().all()
        return {row.comment_id: row.kind for row in rows if row.comment_id is not None}
    return {}


# ---------------------------------------------------------------------------
# Stories
# ---------------------------------------------------------------------------
async def create_story(
    session: AsyncSession,
    *,
    author: User,
    kind: StoryKind,
    media_id: uuid.UUID | None,
    text: str | None,
    settings: Settings,
) -> Story:
    location = _require_home(author)

    if kind == StoryKind.image:
        if media_id is None or text:
            raise BadRequestError("An image story needs an image and no text")
        await claim_assets_for(
            session, media_ids=[media_id], owner_id=author.id, purpose=MediaPurpose.story
        )
    else:
        cleaned = (text or "").strip()
        if not cleaned or media_id is not None:
            raise BadRequestError("A text story needs text and no image")
        if len(cleaned) > settings.story_max_text_length:
            raise BadRequestError(
                f"Text stories are limited to {settings.story_max_text_length} characters",
                details={"max_length": settings.story_max_text_length},
            )
        text = cleaned

    story = Story(
        author_id=author.id,
        kind=kind,
        media_id=media_id if kind == StoryKind.image else None,
        text=text if kind == StoryKind.text else None,
        expires_at=datetime.now(UTC) + settings.story_ttl,
        **location,
    )
    session.add(story)
    await session.flush()
    return story


async def stories_page(
    session: AsyncSession,
    redis: Redis,
    *,
    viewer: User,
    params: PageParams,
    settings: Settings,
    mode: AnchorMode = "home",
) -> StoryPage:
    """Live stories within the viewer's feed radius, newest first.

    Stories share the feed's radius, as the brief's table specifies.
    """
    anchor = await _require_anchor(redis, viewer=viewer, mode=mode)
    radius = await RadiusEngine(session, redis, settings).radius_for(
        RadiusFeature.feed, anchor.cell_res8
    )
    blocked = await blocked_user_ids(session, viewer.id)

    statement: Select[Story] = select(Story).where(
        Story.status == ContentStatus.visible,
        Story.deleted_at.is_(None),
        # Checked on read as well as swept by the worker: between sweeps an
        # expired story must not be served, and the sweep is not instant.
        Story.expires_at > datetime.now(UTC),
        _within(Story.centroid, anchor, radius.radius_m),
    )
    if blocked:
        statement = statement.where(Story.author_id.not_in(blocked))

    stories, next_cursor = await paginate(
        session, statement, sort_column=Story.created_at, id_column=Story.id, params=params
    )
    return StoryPage(stories=stories, next_cursor=next_cursor, anchor=anchor)


async def get_story(session: AsyncSession, *, viewer: User, story_id: uuid.UUID) -> Story:
    story = (
        await session.execute(select(Story).where(Story.id == story_id, Story.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if story is None or story.is_expired(datetime.now(UTC)):
        raise NotFoundError("Story not found")
    if not visible_to(story.status, author_id=story.author_id, viewer_id=viewer.id):
        raise NotFoundError("Story not found")
    if await is_blocked_between(session, viewer.id, story.author_id):
        raise NotFoundError("Story not found")
    return story


async def record_story_view(session: AsyncSession, *, viewer: User, story_id: uuid.UUID) -> Story:
    """Record that the viewer saw a story. Idempotent, and never for the author.

    One row per viewer: the author learns who looked, not how often — counting
    repeat views would make it obvious who keeps coming back, which is more than
    the feature promises.
    """
    story = await get_story(session, viewer=viewer, story_id=story_id)
    if story.author_id == viewer.id:
        return story

    try:
        async with session.begin_nested():
            session.add(StoryView(story_id=story.id, viewer_id=viewer.id))
    except IntegrityError:
        return story

    await session.execute(
        update(Story).where(Story.id == story.id).values(view_count=Story.view_count + 1)
    )
    return story


async def story_viewers(
    session: AsyncSession,
    *,
    viewer: User,
    story_id: uuid.UUID,
    params: PageParams,
) -> tuple[list[tuple[StoryView, User]], str | None]:
    """Who saw a story — for its author only.

    Anyone the author has blocked, or who blocked them, is left out: a block
    means each is invisible to the other, viewer lists included.
    """
    story = await get_story(session, viewer=viewer, story_id=story_id)
    if story.author_id != viewer.id:
        # 404 rather than 403: whether a story exists is all a non-author learns.
        raise NotFoundError("Story not found")

    blocked = await blocked_user_ids(session, viewer.id)
    statement: Select[StoryView] = select(StoryView).where(StoryView.story_id == story.id)
    if blocked:
        statement = statement.where(StoryView.viewer_id.not_in(blocked))

    views, next_cursor = await paginate(
        session,
        statement,
        sort_column=StoryView.created_at,
        id_column=StoryView.id,
        params=params,
    )
    if not views:
        return [], next_cursor
    users = {
        user.id: user
        for user in (
            await session.execute(
                select(User).where(User.id.in_([view.viewer_id for view in views]))
            )
        ).scalars()
    }
    return [(view, users[view.viewer_id]) for view in views if view.viewer_id in users], next_cursor


async def delete_story(session: AsyncSession, *, viewer: User, story_id: uuid.UUID) -> None:
    story = await get_story(session, viewer=viewer, story_id=story_id)
    if story.author_id != viewer.id:
        raise ForbiddenError("Only the author can delete a story")
    story.deleted_at = datetime.now(UTC)
    await session.flush()


# ---------------------------------------------------------------------------
# Account purge
# ---------------------------------------------------------------------------
async def purge_author_content(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, int]:
    """Remove everything a purged account wrote.

    Posts, comments and stories are soft-deleted rather than hard-deleted, so a
    report against one of them still has its evidence until the retention job
    clears it. Reactions and story views carry no content and are simply deleted.

    Called by the account purge. This is one of the two functions the data map
    says every phase introducing personal data must extend.
    """
    now = datetime.now(UTC)
    return {
        "posts": await execute_dml(
            session,
            update(Post)
            .where(Post.author_id == user_id, Post.deleted_at.is_(None))
            .values(deleted_at=now),
        ),
        "comments": await execute_dml(
            session,
            update(Comment)
            .where(Comment.author_id == user_id, Comment.deleted_at.is_(None))
            .values(deleted_at=now),
        ),
        "stories": await execute_dml(
            session,
            update(Story)
            .where(Story.author_id == user_id, Story.deleted_at.is_(None))
            .values(deleted_at=now),
        ),
        "reactions": await execute_dml(
            session, delete(Reaction).where(Reaction.user_id == user_id)
        ),
        "story_views": await execute_dml(
            session, delete(StoryView).where(StoryView.viewer_id == user_id)
        ),
    }


async def export_author_content(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    """Everything the user wrote, for the data export.

    Includes their own soft-deleted content, marked as such: it is still held
    until the retention job purges it, and the right of access is to what is
    held, not to what is shown. Places are given as area IDs, not cells, for the
    same reason the rest of the export avoids them.
    """
    posts = (
        await session.execute(
            select(Post).where(Post.author_id == user_id).order_by(Post.created_at)
        )
    ).scalars()
    comments = (
        await session.execute(
            select(Comment).where(Comment.author_id == user_id).order_by(Comment.created_at)
        )
    ).scalars()
    stories = (
        await session.execute(
            select(Story).where(Story.author_id == user_id).order_by(Story.created_at)
        )
    ).scalars()
    reactions = (
        await session.execute(select(Reaction).where(Reaction.user_id == user_id))
    ).scalars()

    def _when(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "posts": [
            {
                "id": str(post.id),
                "body": post.body,
                "category": post.category,
                "image_media_ids": [str(image.media_id) for image in post.images],
                "status": str(post.status),
                "created_at": _when(post.created_at),
                "deleted_at": _when(post.deleted_at),
            }
            for post in posts
        ],
        "comments": [
            {
                "id": str(comment.id),
                "post_id": str(comment.post_id),
                "parent_id": str(comment.parent_id) if comment.parent_id else None,
                "body": comment.body,
                "written_while_visiting": comment.is_visiting,
                "created_at": _when(comment.created_at),
                "deleted_at": _when(comment.deleted_at),
            }
            for comment in comments
        ],
        "stories": [
            {
                "id": str(story.id),
                "kind": str(story.kind),
                "text": story.text,
                "media_id": str(story.media_id) if story.media_id else None,
                "expires_at": _when(story.expires_at),
                "created_at": _when(story.created_at),
            }
            for story in stories
        ],
        "reactions": [
            {
                "kind": reaction.kind,
                "post_id": str(reaction.post_id) if reaction.post_id else None,
                "comment_id": str(reaction.comment_id) if reaction.comment_id else None,
                "created_at": _when(reaction.created_at),
            }
            for reaction in reactions
        ],
    }
