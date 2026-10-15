"""Marketplace endpoints: listing, browsing, saving, asking and selling."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
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
from app.modules.feed.views import author_view, load_assets, load_users
from app.modules.marketplace import service as marketplace_service
from app.modules.marketplace import views
from app.modules.marketplace.models import Listing, ListingAvailability, ListingCondition
from app.modules.marketplace.schemas import (
    AvailabilityIn,
    BrowseOut,
    BuyerOut,
    ListingCreateIn,
    ListingOut,
    ListingPageOut,
    ListingUpdateIn,
)
from app.modules.messaging import views as messaging_views
from app.modules.messaging.schemas import ConversationOut
from app.modules.users.models import User

router = APIRouter(prefix="/listings", tags=["marketplace"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]

_listing_limit = tiered_user_rate_limit(
    bucket="create_listing",
    window_seconds=86_400,
    new_account_limit=lambda s: s.new_account_listings_per_day,
    established_limit=lambda s: s.listings_per_day,
)
_change_limit = user_rate_limit(bucket="change_listing", limit=200, window_seconds=3600)


async def _one(
    session: SessionDep,
    redis: Redis,
    storage: StorageProvider,
    listing: Listing,
    viewer: User,
    settings: Settings,
) -> ListingOut:
    (rendered,) = await views.listing_views(
        session, redis, storage, listings=[listing], viewer=viewer, settings=settings
    )
    return rendered


@router.post(
    "",
    response_model=ListingOut,
    status_code=status.HTTP_201_CREATED,
    summary="List an item for sale",
    description=(
        "Placed at the seller's home area and shown by its area label — the exact "
        "pickup point is arranged in chat. Expires after 30 days unless renewed. "
        "Returns about how many neighbours can see it.\n\n"
        "Refused with 400 `details.reason = prohibited_item` (naming the term) for "
        "items on the prohibited list, and 409 `details.reason = duplicate` for a "
        "repeat of a listing that is still up. New accounts have a lower daily "
        f"limit.\n\nSend an `{IDEMPOTENCY_HEADER}` header to make retries safe."
    ),
    responses={409: {"model": ErrorEnvelope, "description": "Duplicate, or no home area"}},
    dependencies=[Depends(_listing_limit)],
)
async def create_listing(
    payload: ListingCreateIn,
    request: Request,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> JSONResponse:
    async def action() -> ListingOut:
        listing, _reach = await marketplace_service.create_listing(
            session,
            redis,
            seller=user,
            title=payload.title,
            description=payload.description,
            category=payload.category,
            condition=payload.condition,
            pricing=payload.pricing,
            price_kes=payload.price_kes,
            media_ids=payload.media_ids,
            settings=settings,
        )
        return await _one(session, redis, storage, listing, user, settings)

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
    "",
    response_model=BrowseOut,
    summary="Browse and search listings nearby",
    description=(
        "Listings for sale within the caller's marketplace radius (twice the feed "
        "radius), or their own `distance_km` up to its cap. Available and reserved "
        "items only; sold, expired and hidden ones are left out, as is anyone "
        "blocked.\n\n"
        '`q` is full-text, prefix-matched ("sof" finds "sofa"). Filters: '
        "`category`, `condition`, `min_price`, `max_price`. `sort`: `newest`, "
        "`nearest` (by distance band, then newest), `price_asc`, `price_desc`, or "
        "`relevance` — the default when searching, which returns one page."
    ),
)
async def browse(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    category: str | None = None,
    condition: ListingCondition | None = None,
    min_price: Annotated[int | None, Query(ge=0)] = None,
    max_price: Annotated[int | None, Query(ge=0)] = None,
    sort: Literal["relevance", "newest", "nearest", "price_asc", "price_desc"] | None = None,
    distance_km: Annotated[float | None, Query(gt=0, le=100)] = None,
    anchor: Annotated[Literal["home", "explore"], Query()] = "home",
) -> BrowseOut:
    result = await marketplace_service.browse(
        session,
        redis,
        viewer=user,
        query=q,
        category=category,
        condition=condition,
        min_price=min_price,
        max_price=max_price,
        sort=sort,
        distance_km=distance_km,
        mode=anchor,
        params=page,
        settings=settings,
    )
    return BrowseOut(
        items=await views.listing_views(
            session,
            redis,
            storage,
            listings=result.listings,
            viewer=user,
            settings=settings,
            buckets=result.buckets,
        ),
        next_cursor=result.next_cursor,
        radius_km=round(result.radius_km * 2) / 2,
        sort=sort or ("relevance" if q and q.strip() else "newest"),
    )


@router.get(
    "/mine",
    response_model=ListingPageOut,
    summary="The caller's listings",
    description="Everything the caller is selling or has sold, newest first, expired included.",
)
async def my_listings(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
    availability: ListingAvailability | None = None,
) -> ListingPageOut:
    listings, next_cursor = await marketplace_service.my_listings(
        session, seller=user, availability=availability, params=page
    )
    return ListingPageOut(
        items=await views.listing_views(
            session, redis, storage, listings=listings, viewer=user, settings=settings
        ),
        next_cursor=next_cursor,
    )


@router.get(
    "/saved",
    response_model=ListingPageOut,
    summary="Saved listings",
    description=(
        "Most recently saved first. Sold and expired listings stay, marked as such; "
        "deleted and hidden ones drop out."
    ),
)
async def saved_listings(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
) -> ListingPageOut:
    listings, next_cursor = await marketplace_service.saved_listings(
        session, viewer=user, params=page
    )
    return ListingPageOut(
        items=await views.listing_views(
            session, redis, storage, listings=listings, viewer=user, settings=settings
        ),
        next_cursor=next_cursor,
    )


@router.get(
    "/{listing_id}",
    response_model=ListingOut,
    summary="Get a listing",
    description=(
        "Readable by link even once sold or expired. A listing hidden for review is "
        "visible only to its seller; one removed, or from someone blocked, is a 404."
    ),
)
async def get_listing(
    listing_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ListingOut:
    listing = await marketplace_service.get_listing(session, viewer=user, listing_id=listing_id)
    return await _one(session, redis, storage, listing, user, settings)


@router.patch(
    "/{listing_id}",
    response_model=ListingOut,
    summary="Edit a listing",
    description=(
        "The seller only. The same checks as listing it: prohibited items and "
        "duplicates. `media_ids` replaces the photos. Sold listings cannot be edited."
    ),
    dependencies=[Depends(_change_limit)],
)
async def update_listing(
    listing_id: uuid.UUID,
    payload: ListingUpdateIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ListingOut:
    listing = await marketplace_service.update_listing(
        session,
        seller=user,
        listing_id=listing_id,
        changes=payload.model_dump(exclude_unset=True),
        settings=settings,
    )
    return await _one(session, redis, storage, listing, user, settings)


@router.delete(
    "/{listing_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a listing",
    description="The seller only. Soft, so a report against it keeps its evidence.",
)
async def delete_listing(listing_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await marketplace_service.delete_listing(session, seller=user, listing_id=listing_id)


@router.post(
    "/{listing_id}/renew",
    response_model=ListingOut,
    summary="Renew a listing",
    description=(
        "Another 30 days from now. Possible until 30 days after it expired; after "
        "that, list it again."
    ),
    dependencies=[Depends(_change_limit)],
)
async def renew_listing(
    listing_id: uuid.UUID,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ListingOut:
    listing = await marketplace_service.renew_listing(
        session, seller=user, listing_id=listing_id, settings=settings
    )
    return await _one(session, redis, storage, listing, user, settings)


@router.put(
    "/{listing_id}/availability",
    response_model=ListingOut,
    summary="Mark a listing available, reserved, or sold",
    description=(
        "The seller only. When marking it sold, `buyer_id` may name who bought it — "
        "someone who asked about it in chat. Sold listings leave browse."
    ),
    dependencies=[Depends(_change_limit)],
)
async def set_availability(
    listing_id: uuid.UUID,
    payload: AvailabilityIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> ListingOut:
    listing = await marketplace_service.set_availability(
        session,
        seller=user,
        listing_id=listing_id,
        availability=payload.availability,
        buyer_id=payload.buyer_id,
    )
    return await _one(session, redis, storage, listing, user, settings)


@router.get(
    "/{listing_id}/buyers",
    response_model=list[BuyerOut],
    summary="People who asked about a listing",
    description=(
        "The seller only, most recent conversation first — for choosing who it was sold to."
    ),
)
async def listing_buyers(
    listing_id: uuid.UUID, user: CurrentUser, session: SessionDep, storage: StorageDep
) -> list[BuyerOut]:
    rows = await marketplace_service.listing_buyers(session, seller=user, listing_id=listing_id)
    users = await load_users(session, (buyer for buyer, _c, _s in rows))
    assets = await load_assets(session, [person.avatar_media_id for person in users.values()])
    return [
        BuyerOut(
            user=author_view(users[buyer], assets, storage),
            conversation_id=conversation_id,
            accepted=state == "active",
        )
        for buyer, conversation_id, state in rows
        if buyer in users
    ]


@router.put(
    "/{listing_id}/save",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Save a listing",
    description="Idempotent.",
    dependencies=[Depends(user_rate_limit(bucket="save_listing", limit=300, window_seconds=3600))],
)
async def save_listing(listing_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await marketplace_service.save_listing(session, viewer=user, listing_id=listing_id)


@router.delete(
    "/{listing_id}/save",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unsave a listing",
    description="Idempotent.",
)
async def unsave_listing(listing_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await marketplace_service.unsave_listing(session, viewer=user, listing_id=listing_id)


@router.post(
    "/{listing_id}/chat",
    response_model=ConversationOut,
    summary="Chat with the seller",
    description=(
        "Opens the caller's one conversation with the seller about this listing, "
        "creating it if needed; then send messages to it as usual. As with any first "
        "contact, the buyer can send one message until the seller replies. It lands "
        "in the seller's inbox, not their requests. Not for listings already sold "
        "or expired, unless the conversation already exists."
    ),
    dependencies=[Depends(user_rate_limit(bucket="listing_chat", limit=100, window_seconds=3600))],
)
async def open_chat(
    listing_id: uuid.UUID,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
) -> ConversationOut:
    conversation = await marketplace_service.open_chat(session, buyer=user, listing_id=listing_id)
    (rendered,) = await messaging_views.conversation_views(
        session, storage, conversations=[conversation], viewer=user
    )
    return rendered
