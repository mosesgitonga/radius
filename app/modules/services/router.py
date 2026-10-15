"""Endpoints for services and jobs, and for reviews."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.errors import ErrorEnvelope
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.core.redis import get_redis
from app.modules.auth.dependencies import (
    ActiveUser,
    CurrentUser,
    tiered_user_rate_limit,
    user_rate_limit,
)
from app.modules.messaging import views as messaging_views
from app.modules.messaging.schemas import ConversationOut
from app.modules.services import reviews as reviews_service
from app.modules.services import service as services_service
from app.modules.services import views
from app.modules.services.models import ServiceKind, ServicePost
from app.modules.services.schemas import (
    OpenIn,
    ReplyIn,
    ReviewIn,
    ReviewOut,
    ReviewPageOut,
    ReviewUpdateIn,
    ServiceBrowseOut,
    ServicePostIn,
    ServicePostOut,
    ServicePostPageOut,
    ServicePostUpdateIn,
)
from app.modules.users.models import User

router = APIRouter(tags=["services"])
reviews_router = APIRouter(tags=["reviews"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]

_post_limit = tiered_user_rate_limit(
    bucket="create_service_post",
    window_seconds=86_400,
    new_account_limit=lambda s: s.new_account_service_posts_per_day,
    established_limit=lambda s: s.service_posts_per_day,
)
_review_limit = tiered_user_rate_limit(
    bucket="create_review",
    window_seconds=86_400,
    new_account_limit=lambda s: s.reviews_per_day,
    established_limit=lambda s: s.reviews_per_day,
)


async def _one(
    session: SessionDep,
    redis: Redis,
    storage: StorageProvider,
    post: ServicePost,
    viewer: User,
    settings: Settings,
) -> ServicePostOut:
    (rendered,) = await views.post_views(
        session, redis, storage, posts=[post], viewer=viewer, settings=settings
    )
    return rendered


# ---------------------------------------------------------------------------
# Service posts
# ---------------------------------------------------------------------------
@router.post(
    "/services",
    response_model=ServicePostOut,
    status_code=status.HTTP_201_CREATED,
    summary="Offer a service, ask for one, or post a job",
    description=(
        "`offering` is a provider profile for one category (one live offering per "
        "category); `seeking` asks for help; `hiring` advertises a job. Each kind "
        "takes its own fields; another kind's fields are refused.\n\n"
        "**Jobs carry guard rails.** Posting one needs a verified phone and an account "
        "at least `HIRING_MIN_ACCOUNT_AGE_DAYS` old (403, `details.reason = "
        "hiring_trust_threshold`). A job post that asks the job seeker for money — "
        '"registration fee", "ada ya usajili" — is created but **held for review**: '
        "`status` is `hidden_pending_review`, and `held_for` lists the phrases."
    ),
    responses={409: {"model": ErrorEnvelope, "description": "Duplicate offering, or no home"}},
    dependencies=[Depends(_post_limit)],
)
async def create_post(
    payload: ServicePostIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ServicePostOut:
    post = await services_service.create_post(
        session,
        author=user,
        kind=payload.kind,
        fields=payload.model_dump(exclude={"kind"}, exclude_unset=True),
        settings=settings,
    )
    return await _one(session, redis, storage, post, user, settings)


@router.get(
    "/services",
    response_model=ServiceBrowseOut,
    summary="Find services, needs and jobs nearby",
    description=(
        "Open posts within the caller's services radius (three times the feed "
        "radius), or their own `distance_km` up to its cap. Filter by `kind` and "
        "`category`; `q` is full-text and prefix-matched. `sort`: `newest`, `nearest` "
        "(by distance band), or `relevance` — the default when searching, one page."
    ),
)
async def browse(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
    kind: ServiceKind | None = None,
    category: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: Literal["relevance", "newest", "nearest"] | None = None,
    distance_km: Annotated[float | None, Query(gt=0, le=100)] = None,
    anchor: Annotated[Literal["home", "explore"], Query()] = "home",
) -> ServiceBrowseOut:
    result = await services_service.browse(
        session,
        redis,
        viewer=user,
        kind=kind,
        category=category,
        query=q,
        sort=sort,
        distance_km=distance_km,
        mode=anchor,
        params=page,
        settings=settings,
    )
    return ServiceBrowseOut(
        items=await views.post_views(
            session,
            redis,
            storage,
            posts=result.posts,
            viewer=user,
            settings=settings,
            buckets=result.buckets,
        ),
        next_cursor=result.next_cursor,
        radius_km=round(result.radius_km * 2) / 2,
        sort=result.sort,
    )


@router.get(
    "/services/mine",
    response_model=ServicePostPageOut,
    summary="The caller's service posts",
    description="Open, closed, expired and held, newest first.",
)
async def my_posts(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
) -> ServicePostPageOut:
    posts, next_cursor = await services_service.my_posts(session, author=user, params=page)
    return ServicePostPageOut(
        items=await views.post_views(
            session, redis, storage, posts=posts, viewer=user, settings=settings
        ),
        next_cursor=next_cursor,
    )


@router.get(
    "/services/{post_id}",
    response_model=ServicePostOut,
    summary="Get a service post",
    description="Readable by link when open or closed. A held post is visible only to its author.",
)
async def get_post(
    post_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ServicePostOut:
    post = await services_service.get_post(session, viewer=user, post_id=post_id)
    return await _one(session, redis, storage, post, user, settings)


@router.patch(
    "/services/{post_id}",
    response_model=ServicePostOut,
    summary="Edit a service post",
    description=(
        "The author only. Job posts are checked again for fee demands. A held post "
        "stays held after editing: a moderator releases it, not an edit."
    ),
    dependencies=[Depends(user_rate_limit(bucket="edit_service", limit=100, window_seconds=3600))],
)
async def update_post(
    post_id: uuid.UUID,
    payload: ServicePostUpdateIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ServicePostOut:
    post = await services_service.update_post(
        session,
        author=user,
        post_id=post_id,
        changes=payload.model_dump(exclude_unset=True),
        settings=settings,
    )
    return await _one(session, redis, storage, post, user, settings)


@router.delete(
    "/services/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a service post",
    description="The author only. Soft, as for all content.",
)
async def delete_post(post_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await services_service.delete_post(session, author=user, post_id=post_id)


@router.put(
    "/services/{post_id}/open",
    response_model=ServicePostOut,
    summary="Close or reopen a service post",
    description="Close when the job is filled or the need met; reopening gives a fresh 30 days.",
)
async def set_open(
    post_id: uuid.UUID,
    payload: OpenIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ServicePostOut:
    post = await services_service.set_open(
        session, author=user, post_id=post_id, open_=payload.open, settings=settings
    )
    return await _one(session, redis, storage, post, user, settings)


@router.post(
    "/services/{post_id}/chat",
    response_model=ConversationOut,
    summary="Contact the author of a service post",
    description=(
        "Opens the caller's one conversation with the author about this post. As for "
        "any first contact, one message until they reply; it lands in their inbox. A "
        "conversation about a job carries a `safety_notice` the client must show."
    ),
    dependencies=[Depends(user_rate_limit(bucket="service_chat", limit=100, window_seconds=3600))],
)
async def open_chat(
    post_id: uuid.UUID, user: ActiveUser, session: SessionDep, storage: StorageDep
) -> ConversationOut:
    conversation = await services_service.open_chat(session, contacter=user, post_id=post_id)
    (rendered,) = await messaging_views.conversation_views(
        session, storage, conversations=[conversation], viewer=user
    )
    return rendered


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------
@reviews_router.post(
    "/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="Review someone",
    description=(
        "1-5 stars and optional text, about one listing or service post. Allowed only "
        "if the two of you had an **accepted** conversation about exactly that — one "
        "they replied to (403, `details.reason = no_qualifying_conversation`, "
        "otherwise). One review per person, per person reviewed, per context (409 on "
        "a second)."
    ),
    dependencies=[Depends(_review_limit)],
)
async def create_review(
    payload: ReviewIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ReviewOut:
    review = await reviews_service.create_review(
        session,
        reviewer=user,
        subject_id=payload.subject_id,
        context_type=payload.context_type,
        context_id=payload.context_id,
        rating=payload.rating,
        body=payload.body,
        settings=settings,
    )
    (rendered,) = await views.review_views(session, storage, reviews=[review])
    return rendered


@reviews_router.get(
    "/users/{user_id}/reviews",
    response_model=ReviewPageOut,
    summary="Someone's reviews and rating",
    description=(
        "Newest first, with their cached average and count. Reviews by anyone blocked "
        "either way are left out."
    ),
)
async def reviews_of(
    user_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    page: PageDep,
) -> ReviewPageOut:
    subject, rows, next_cursor = await reviews_service.reviews_of(
        session, viewer=user, subject_id=user_id, params=page
    )
    return ReviewPageOut(
        items=await views.review_views(session, storage, reviews=rows),
        next_cursor=next_cursor,
        rating_average=float(subject.rating_average) if subject.rating_average else None,
        rating_count=subject.rating_count,
    )


@reviews_router.get(
    "/reviews/{review_id}",
    response_model=ReviewOut,
    summary="Get a review",
    description="Hidden reviews are visible only to their author.",
)
async def get_review(
    review_id: uuid.UUID, user: CurrentUser, session: SessionDep, storage: StorageDep
) -> ReviewOut:
    review = await reviews_service.get_review(session, viewer=user, review_id=review_id)
    (rendered,) = await views.review_views(session, storage, reviews=[review])
    return rendered


@reviews_router.patch(
    "/reviews/{review_id}",
    response_model=ReviewOut,
    summary="Edit your review",
    description="The rating is recomputed at once.",
)
async def update_review(
    review_id: uuid.UUID,
    payload: ReviewUpdateIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ReviewOut:
    review = await reviews_service.update_review(
        session,
        reviewer=user,
        review_id=review_id,
        rating=payload.rating,
        body=payload.body,
        settings=settings,
    )
    (rendered,) = await views.review_views(session, storage, reviews=[review])
    return rendered


@reviews_router.delete(
    "/reviews/{review_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete your review",
    description="You may then write it again.",
)
async def delete_review(review_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await reviews_service.delete_review(session, reviewer=user, review_id=review_id)


@reviews_router.put(
    "/reviews/{review_id}/reply",
    response_model=ReviewOut,
    summary="Reply to a review of you",
    description="One reply, which you can edit. The reviewer is notified the first time.",
)
async def reply(
    review_id: uuid.UUID,
    payload: ReplyIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ReviewOut:
    review = await reviews_service.reply(
        session, subject=user, review_id=review_id, body=payload.body, settings=settings
    )
    (rendered,) = await views.review_views(session, storage, reviews=[review])
    return rendered


@reviews_router.delete(
    "/reviews/{review_id}/reply",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove your reply",
    description="Idempotent.",
)
async def delete_reply(
    review_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> None:
    await reviews_service.reply(
        session, subject=user, review_id=review_id, body=None, settings=settings
    )
