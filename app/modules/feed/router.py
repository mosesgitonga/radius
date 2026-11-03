"""Feed endpoints: posts, the feed, comments, reactions, and stories."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.errors import ErrorEnvelope
from app.core.idempotency import IDEMPOTENCY_HEADER, execute_idempotently
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.core.ratelimit import rate_limit
from app.core.redis import get_redis
from app.modules.auth.dependencies import (
    ActiveUser,
    CurrentUser,
    tiered_user_rate_limit,
    user_rate_limit,
)
from app.modules.feed import guest, views
from app.modules.feed import service as feed_service
from app.modules.feed.models import Post, Story, StoryKind
from app.modules.feed.schemas import (
    CommentCreateIn,
    CommentOut,
    CommentPageOut,
    FeedOut,
    GuestFeedIn,
    GuestFeedOut,
    PostCreatedOut,
    PostCreateIn,
    PostOut,
    PostPageOut,
    ReactionIn,
    ReactionOut,
    StoryCreateIn,
    StoryOut,
    StoryPageOut,
    StoryViewerOut,
    StoryViewerPageOut,
)
from app.modules.users import service as users_service
from app.modules.users.models import User
from app.modules.users.schemas import AreaLabelOut

router = APIRouter(tags=["feed"])

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
    beyond = (
        await feed_service.beyond_feed(
            session, redis, viewer=viewer, model=Post, items=[post], settings=settings
        )
        if post.group_id is None
        else set()
    )
    (rendered,) = await views.post_views(
        session,
        redis,
        storage,
        posts=[post],
        viewer=viewer,
        viewer_cell_res8=viewer.home_cell_res8,
        settings=settings,
        beyond=beyond,
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
        "`audience` decides whether the author's circle sees it too, wherever they live; "
        "`service_post_id` posts it as one of the author's service offerings, so the "
        "service's followers see it in their circle feed.\n\n"
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
            audience=payload.audience,
            service_post_id=payload.service_post_id,
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


@router.post(
    "/guest/feed",
    response_model=GuestFeedOut,
    summary="The feed, before signing up",
    description=(
        "For someone who has not signed up: the posts a member standing at the "
        "phone's GPS position would see in their feed, newest first, with **no "
        "author**: no name, photo or account ID (decision M.9). The reading follows "
        "the home rules (accurate to `LOCATION_MAX_ACCURACY_METRES`, never faked) and "
        "is discarded once turned into a cell. Nothing about the visitor is stored. "
        "Coordinates go in the body so they never reach an access log.\n\n"
        "No account needed. Limited per IP to 30 requests a minute and 300 an hour."
    ),
    tags=["guest"],
    dependencies=[
        Depends(rate_limit(bucket="guest_feed_minute", limit=30, window_seconds=60, scope="ip")),
        Depends(rate_limit(bucket="guest_feed_hour", limit=300, window_seconds=3600, scope="ip")),
    ],
)
async def get_guest_feed(
    payload: GuestFeedIn,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GuestFeedOut:
    params = PageParams(limit=settings.pagination_default_limit, cursor=payload.cursor)
    page = await guest.guest_feed_page(
        session,
        redis,
        latitude=payload.latitude,
        longitude=payload.longitude,
        accuracy_metres=payload.accuracy_metres,
        is_mocked=payload.is_mocked,
        params=params,
        settings=settings,
    )
    return GuestFeedOut(
        items=await views.guest_post_views(
            session,
            redis,
            storage,
            posts=page.posts,
            viewer_cell_res8=page.anchor.cell_res8,
            settings=settings,
        ),
        next_cursor=page.next_cursor,
        area_label=AreaLabelOut(label=page.area.label, precision=page.area.precision),
        radius_km=page.radius.display_km(settings.radius_display_rounding_km),
    )


@router.get(
    "/feed/circle",
    response_model=PostPageOut,
    summary="My circle's feed",
    description=(
        "Posts from people in my circle and from services I follow, from anywhere, "
        "newest first. Only posts shared beyond the neighbourhood "
        "(`audience = neighbours_and_circle`); never the provider's own posts for a "
        "followed service, only those made as the service. Excludes anyone either "
        "party has blocked, and anything hidden or removed. A post beyond my feed "
        "radius shows its town only and no distance. The neighbourhood feed, "
        "`GET /feed`, is unchanged by any of this."
    ),
)
async def get_circle_feed(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    params: PageDep,
) -> PostPageOut:
    posts, next_cursor = await feed_service.circle_feed_page(session, viewer=user, params=params)
    beyond = await feed_service.beyond_feed(
        session, redis, viewer=user, model=Post, items=posts, settings=settings
    )
    return PostPageOut(
        items=await views.post_views(
            session,
            redis,
            storage,
            posts=posts,
            viewer=user,
            viewer_cell_res8=user.home_cell_res8,
            settings=settings,
            beyond=beyond,
        ),
        next_cursor=next_cursor,
    )


@router.get(
    "/users/{user_id}/posts",
    response_model=PostPageOut,
    summary="A neighbour's posts, for their profile",
    description=(
        "Newest first. To the person themselves: all their posts, held ones "
        "included. To anyone else: only posts their feed could show them — within "
        "their feed radius of home — so a profile surfaces nothing the feed would "
        "not. Someone in the person's circle also sees what they shared with their "
        "circle, wherever they live, by town and with no distance. The profile's "
        "`posts_visible` says whether to expect any. 404 if either party has blocked "
        "the other, as for the profile."
    ),
)
async def get_profile_posts(
    user_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    params: PageDep,
) -> PostPageOut:
    author = await users_service.profile_owner(session, viewer=user, user_id=user_id)
    posts, next_cursor = await feed_service.profile_posts(
        session, redis, viewer=user, author=author, params=params, settings=settings
    )
    beyond = await feed_service.beyond_feed(
        session, redis, viewer=user, model=Post, items=posts, settings=settings
    )
    return PostPageOut(
        items=await views.post_views(
            session,
            redis,
            storage,
            posts=posts,
            viewer=user,
            viewer_cell_res8=user.home_cell_res8,
            settings=settings,
            beyond=beyond,
        ),
        next_cursor=next_cursor,
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
        "Placed in the caller's home area. `audience` decides whether the caller's "
        "circle sees it too, wherever they live." + IDEMPOTENCY_DOC
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
            audience=payload.audience,
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


@router.get(
    "/stories/circle",
    response_model=StoryPageOut,
    summary="My circle's stories",
    description=(
        "Live stories from people in my circle, from anywhere, newest first: only "
        "those shared beyond the neighbourhood. A story beyond my feed radius shows "
        "its town only and no distance."
    ),
)
async def list_circle_stories(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    params: PageDep,
) -> StoryPageOut:
    stories, next_cursor = await feed_service.circle_stories_page(
        session, viewer=user, params=params
    )
    beyond = await feed_service.beyond_feed(
        session, redis, viewer=user, model=Story, items=stories, settings=settings
    )
    return StoryPageOut(
        items=await views.story_views(
            session,
            redis,
            storage,
            stories=stories,
            viewer=user,
            viewer_cell_res8=user.home_cell_res8,
            settings=settings,
            beyond=beyond,
        ),
        next_cursor=next_cursor,
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
