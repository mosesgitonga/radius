"""Follow endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.modules.auth.dependencies import ActiveUser, CurrentUser
from app.modules.feed.views import author_view, load_assets, load_users
from app.modules.follows import links
from app.modules.follows import service as follows_service
from app.modules.follows.models import Follow, FollowEntityType
from app.modules.follows.schemas import FollowedServiceOut, FollowIn, FollowOut, FollowPageOut
from app.modules.services.models import ServicePost

router = APIRouter(tags=["follows"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]


async def _views(
    session: AsyncSession, storage: StorageProvider, follows: Sequence[Follow]
) -> list[FollowOut]:
    ids = [row.entity_id for row in follows]
    counts = await links.follower_counts(session, FollowEntityType.service_provider, ids)
    services = {
        post.id: post
        for post in (
            await session.execute(select(ServicePost).where(ServicePost.id.in_(ids)))
        ).scalars()
    }
    providers = await load_users(session, (post.author_id for post in services.values()))
    assets = await load_assets(session, (user.avatar_media_id for user in providers.values()))
    views: list[FollowOut] = []
    for row in follows:
        post = services.get(row.entity_id)
        provider = providers.get(post.author_id) if post is not None else None
        views.append(
            FollowOut(
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                followed_at=row.created_at,
                follower_count=counts.get(row.entity_id, 0),
                service=(
                    FollowedServiceOut(
                        id=post.id,
                        title=post.title,
                        category=post.category,
                        provider=author_view(provider, assets, storage),
                    )
                    if post is not None and provider is not None
                    else None
                ),
            )
        )
    return views


@router.post(
    "/follows",
    response_model=FollowOut,
    status_code=status.HTTP_201_CREATED,
    summary="Follow a service",
    description=(
        "Follow a service provider's offering. Posts the provider makes as that service "
        "then appear in `GET /feed/circle`, wherever either of you lives. People cannot "
        "be followed: `entity_type` accepts only `service_provider` (businesses will "
        "follow in Phase 10). Following again is harmless and returns the same follow. "
        "404 for a service that is gone, held, or whose provider is blocked either way. "
        "At most `FOLLOWS_PER_DAY` new follows a day."
    ),
)
async def follow(
    payload: FollowIn,
    user: ActiveUser,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> FollowOut:
    record = await follows_service.follow(
        session,
        user=user,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        settings=settings,
    )
    (view,) = await _views(session, storage, [record])
    return view


@router.delete(
    "/follows/{entity_type}/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop following",
    description="Nobody is told. Unfollowing something you do not follow is not an error.",
)
async def unfollow(
    entity_type: FollowEntityType, entity_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await follows_service.unfollow(session, user=user, entity_type=entity_type, entity_id=entity_id)


@router.get(
    "/me/follows",
    response_model=FollowPageOut,
    summary="What I follow",
    description=(
        "Newest first. Services that have gone, or whose provider is blocked either way, "
        "are left out. There is no list of who follows you: only counts are shown."
    ),
)
async def my_follows(
    user: CurrentUser, session: SessionDep, storage: StorageDep, params: PageDep
) -> FollowPageOut:
    rows, next_cursor = await follows_service.my_follows(session, user=user, params=params)
    return FollowPageOut(items=await _views(session, storage, rows), next_cursor=next_cursor)
