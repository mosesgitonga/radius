"""Feed endpoints: posts, the feed, comments, reactions, and stories."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import ErrorEnvelope
from app.core.idempotency import IDEMPOTENCY_HEADER, execute_idempotently
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.core.redis import get_redis
from app.modules.auth.dependencies import (
    ActiveUser,
    CurrentUser,
    tiered_user_rate_limit,
    user_rate_limit,
)
from app.modules.feed import service as feed_service
from app.modules.feed import views
from app.modules.feed.models import Post, StoryKind
from app.modules.feed.schemas import (
    CommentCreateIn,
    CommentOut,
    CommentPageOut,
    FeedOut,
    PostCreatedOut,
    PostCreateIn,
    PostOut,
    ReactionIn,
    ReactionOut,
    StoryCreateIn,
    StoryOut,
    StoryPageOut,
    StoryViewerOut,
    StoryViewerPageOut,
)
from app.modules.users.models import User

router = APIRouter(tags=["feed"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]
AnchorQuery = Annotated[
    Literal["home", "explore"],
    Query(description="Browse from the home area, or from an active explore session."),
]

IDEMPOTENCY_DOC = (
    f"\n\nSend an `{IDEMPOTENCY_HEADER}` header (a fresh UUID per action) to make "
    "retries safe: a repeat with the same key returns the original response, marked "
    "`Idempotent-Replayed: true`, and creates nothing new."
)

_post_limit = tiered_user_rate_limit(
    bucket="create_post",
    window_seconds=86_400,
    new_account_limit=lambda s: s.new_account_posts_per_day,
    established_limit=lambda s: s.posts_per_day,
)
_comment_limit = tiered_user_rate_limit(
    bucket="create_comment",
    window_seconds=3600,
    new_account_limit=lambda s: s.new_account_comments_per_hour,
    established_limit=lambda s: s.comments_per_hour,
)
_story_limit = tiered_user_rate_limit(
    bucket="create_story",
    window_seconds=86_400,
    new_account_limit=lambda s: s.new_account_stories_per_day,
    established_limit=lambda s: s.stories_per_day,
)
_reaction_limit = user_rate_limit(bucket="react", limit=300, window_seconds=3600)


async def _one_post_view(
    session: AsyncSession,
    redis: Redis,
    storage: StorageProvider,
    *,
    post: Post,
    viewer: User,
    settings: Settings,
) -> PostOut:
    (rendered,) = await views.post_views(
        session,
        redis,
        storage,
        posts=[post],
        viewer=viewer,
        viewer_cell_res8=viewer.home_cell_res8,
        settings=settings,
    )
    return rendered


# ---------------------------------------------------------------------------
# Posts and the feed
# ---------------------------------------------------------------------------
@router.post(
    "/posts",
    response_model=PostCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a post",
    description=(
        "Posts to the caller's **home** area, even while exploring elsewhere — that "
        "is what stops posts being dropped into neighbourhoods somebody does not live "
        "in. Returns an estimate of how many active neighbours can see it.\n\n"
        "New accounts have a lower daily limit than established ones." + IDEMPOTENCY_DOC
    ),
    dependencies=[Depends(_post_limit)],
)
async def create_post(
    payload: PostCreateIn,
    request: Request,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> JSONResponse:
    async def action() -> PostCreatedOut:
        post, reach = await feed_service.create_post(
            session,
            redis,
            author=user,
            body=payload.body,
            category=payload.category,
            media_ids=payload.media_ids,
            settings=settings,
        )
        await session.refresh(post, attribute_names=["images", "created_at"])
        return PostCreatedOut(
            post=await _one_post_view(
                session, redis, storage, post=post, viewer=user, settings=settings
            ),
            reach_estimate=reach,
        )

    return await execute_idempotently(
        session,
        request=request,
        user_id=user.id,
        payload=payload.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
        action=action,
        settings=settings,
    )


@router.get(
    "/feed",
    response_model=FeedOut,
    summary="The neighbourhood feed",
    description=(
        "Posts within the caller's adaptive radius, newest first. Excludes anyone "
        "either party has blocked, and anything hidden or removed.\n\n"
        "`sort` is always `recent` today; the field exists so ranking can arrive "
        "without changing the response."
    ),
    responses={409: {"model": ErrorEnvelope, "description": "No home area set yet"}},
)
async def get_feed(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    params: PageDep,
    anchor: AnchorQuery = "home",
) -> FeedOut:
    page = await feed_service.feed_page(
        session, redis, viewer=user, params=params, settings=settings, mode=anchor
    )
    return FeedOut(
        items=await views.post_views(
            session,
            redis,
            storage,
            posts=page.posts,
            viewer=user,
            viewer_cell_res8=page.anchor.cell_res8,
            settings=settings,
        ),
        next_cursor=page.next_cursor,
        radius_km=page.radius.display_km(settings.radius_display_rounding_km),
    )


@router.get(
    "/posts/{post_id}",
    response_model=PostOut,
    summary="One post",
    description=(
        "Readable by link regardless of radius — the radius governs what the feed "
        "*surfaces*, not what may be opened. Returns 404 if either party has blocked "
        "the other."
    ),
)
async def get_post(
    post_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> PostOut:
    post = await feed_service.get_post(session, viewer=user, post_id=post_id)
    return await _one_post_view(session, redis, storage, post=post, viewer=user, settings=settings)


@router.delete(
    "/posts/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete my post",
    description="Soft-deleted, so a report against it keeps its evidence until review.",
)
async def delete_post(post_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await feed_service.delete_post(session, viewer=user, post_id=post_id)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
@router.post(
    "/posts/{post_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Comment on a post",
    description=(
        "Pass `parent_id` to reply to a top-level comment. Replies to replies are "
        "refused: one level only. A comment written while exploring is marked "
        "`is_visiting`." + IDEMPOTENCY_DOC
    ),
    dependencies=[Depends(_comment_limit)],
)
async def add_comment(
    post_id: uuid.UUID,
    payload: CommentCreateIn,
    request: Request,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> JSONResponse:
    async def action() -> CommentOut:
        comment = await feed_service.add_comment(
            session,
            redis,
            author=user,
            post_id=post_id,
            body=payload.body,
            parent_id=payload.parent_id,
            settings=settings,
        )
        await session.refresh(comment)
        (rendered,) = await views.comment_views(
            session, redis, storage, comments=[comment], viewer=user, settings=settings
        )
        return rendered

    return await execute_idempotently(
        session,
        request=request,
        user_id=user.id,
        payload=payload.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
        action=action,
        settings=settings,
    )


@router.get(
    "/posts/{post_id}/comments",
    response_model=CommentPageOut,
    summary="Comments on a post",
    description=(
        "Top-level comments oldest first, or the replies to one with `parent_id`. "
        "Excludes anyone either party has blocked."
    ),
)
async def list_comments(
    post_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    params: PageDep,
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
) -> CommentPageOut:
    comments, next_cursor = await feed_service.list_comments(
        session, viewer=user, post_id=post_id, parent_id=parent_id, params=params
    )
    return CommentPageOut(
        items=await views.comment_views(
            session, redis, storage, comments=comments, viewer=user, settings=settings
        ),
        next_cursor=next_cursor,
    )


@router.delete(
    "/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a comment",
    description=(
        "The comment's author may delete it, and so may the author of the post it is "
        "on — so a neighbour can clear abuse from under their own post without "
        "waiting for a moderator."
    ),
)
async def delete_comment(comment_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await feed_service.delete_comment(session, viewer=user, comment_id=comment_id)


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------
@router.put(
    "/posts/{post_id}/reaction",
    response_model=ReactionOut,
    summary="React to a post",
    description="One reaction per person per post; sending another replaces it.",
    dependencies=[Depends(_reaction_limit)],
)
async def react_to_post(
    post_id: uuid.UUID,
    payload: ReactionIn,
    user: ActiveUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ReactionOut:
    kind, count = await feed_service.set_reaction(
        session, user=user, kind=payload.kind, settings=settings, post_id=post_id
    )
    return ReactionOut(kind=kind, reaction_count=count)


@router.delete(
    "/posts/{post_id}/reaction",
    response_model=ReactionOut,
    summary="Remove my reaction to a post",
    description="Succeeds if there was no reaction, since the outcome already holds.",
)
async def unreact_to_post(
    post_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> ReactionOut:
    count = await feed_service.remove_reaction(session, user=user, post_id=post_id)
    return ReactionOut(kind=None, reaction_count=count)


@router.put(
    "/comments/{comment_id}/reaction",
    response_model=ReactionOut,
    summary="React to a comment",
    description="One reaction per person per comment; sending another replaces it.",
    dependencies=[Depends(_reaction_limit)],
)
async def react_to_comment(
    comment_id: uuid.UUID,
    payload: ReactionIn,
    user: ActiveUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ReactionOut:
    kind, count = await feed_service.set_reaction(
        session, user=user, kind=payload.kind, settings=settings, comment_id=comment_id
    )
    return ReactionOut(kind=kind, reaction_count=count)


@router.delete(
    "/comments/{comment_id}/reaction",
    response_model=ReactionOut,
    summary="Remove my reaction to a comment",
    description="Succeeds if there was no reaction, since the outcome already holds.",
)
async def unreact_to_comment(
    comment_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> ReactionOut:
    count = await feed_service.remove_reaction(session, user=user, comment_id=comment_id)
    return ReactionOut(kind=None, reaction_count=count)


# ---------------------------------------------------------------------------
# Stories
# ---------------------------------------------------------------------------
@router.post(
    "/stories",
    response_model=StoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post a story",
    description=(
        "An image (a ready `story` upload) or a line of text, gone after 24 hours. "
        "Placed in the caller's home area." + IDEMPOTENCY_DOC
    ),
    dependencies=[Depends(_story_limit)],
)
async def create_story(
    payload: StoryCreateIn,
    request: Request,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> JSONResponse:
    async def action() -> StoryOut:
        story = await feed_service.create_story(
            session,
            author=user,
            kind=StoryKind(payload.kind),
            media_id=payload.media_id,
            text=payload.text,
            settings=settings,
        )
        await session.refresh(story)
        (rendered,) = await views.story_views(
            session,
            redis,
            storage,
            stories=[story],
            viewer=user,
            viewer_cell_res8=user.home_cell_res8,
            settings=settings,
        )
        return rendered

    return await execute_idempotently(
        session,
        request=request,
        user_id=user.id,
        payload=payload.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
        action=action,
        settings=settings,
    )


@router.get(
    "/stories",
    response_model=StoryPageOut,
    summary="Stories nearby",
    description="Live stories within the caller's feed radius, newest first.",
)
async def list_stories(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    params: PageDep,
    anchor: AnchorQuery = "home",
) -> StoryPageOut:
    page = await feed_service.stories_page(
        session, redis, viewer=user, params=params, settings=settings, mode=anchor
    )
    return StoryPageOut(
        items=await views.story_views(
            session,
            redis,
            storage,
            stories=page.stories,
            viewer=user,
            viewer_cell_res8=page.anchor.cell_res8,
            settings=settings,
        ),
        next_cursor=page.next_cursor,
    )


@router.post(
    "/stories/{story_id}/view",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark a story as seen",
    description="Idempotent. The author's own views are not recorded.",
)
async def view_story(story_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await feed_service.record_story_view(session, viewer=user, story_id=story_id)


@router.get(
    "/stories/{story_id}/viewers",
    response_model=StoryViewerPageOut,
    summary="Who saw my story",
    description="The story's author only. Anyone either party has blocked is left out.",
)
async def story_viewers(
    story_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    params: PageDep,
) -> StoryViewerPageOut:
    rows, next_cursor = await feed_service.story_viewers(
        session, viewer=user, story_id=story_id, params=params
    )
    authors = await views.author_views_for(session, storage, [viewer for _, viewer in rows])
    return StoryViewerPageOut(
        items=[
            StoryViewerOut(user=authors[viewer.id], viewed_at=view.created_at)
            for view, viewer in rows
        ],
        next_cursor=next_cursor,
    )


@router.delete(
    "/stories/{story_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete my story",
    description="Removes it before its 24 hours are up. The author only.",
)
async def delete_story(story_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await feed_service.delete_story(session, viewer=user, story_id=story_id)
