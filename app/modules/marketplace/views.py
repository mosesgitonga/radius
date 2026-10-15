"""Turning listings into responses, in batches."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.storage import StorageProvider
from app.modules.feed.schemas import PostImageOut
from app.modules.feed.views import author_view, load_assets, load_users
from app.modules.location import cells
from app.modules.location.service import area_label_for_cell
from app.modules.marketplace import service as marketplace_service
from app.modules.marketplace.models import Listing
from app.modules.marketplace.schemas import ListingOut
from app.modules.media.views import image_variants
from app.modules.users.models import User
from app.modules.users.schemas import AreaLabelOut


async def listing_views(
    session: AsyncSession,
    redis: Redis,
    storage: StorageProvider,
    *,
    listings: Sequence[Listing],
    viewer: User,
    settings: Settings,
    buckets: dict[uuid.UUID, cells.DistanceBucket] | None = None,
) -> list[ListingOut]:
    """`buckets` from a browse query, when there is one, so the band shown is the
    one the list was sorted by; otherwise computed from the coarse cells."""
    if not listings:
        return []
    ids = [listing.id for listing in listings]
    sellers = await load_users(session, (listing.seller_id for listing in listings))
    assets = await load_assets(
        session,
        [image.media_id for listing in listings for image in listing.images]
        + [user.avatar_media_id for user in sellers.values()],
    )
    saved = await marketplace_service.saved_ids(session, viewer=viewer, listing_ids=ids)
    chats = await marketplace_service.my_conversations_about(
        session, viewer=viewer, listing_ids=ids
    )
    labels: dict[int, AreaLabelOut] = {}
    for cell in {listing.cell_res9 for listing in listings}:
        label = await area_label_for_cell(session, redis, cell_res9=cell, settings=settings)
        labels[cell] = AreaLabelOut(label=label.label, precision=label.precision)

    now = datetime.now(UTC)
    views: list[ListingOut] = []
    for listing in listings:
        seller = sellers.get(listing.seller_id)
        if seller is None:  # pragma: no cover — the seller row cascades
            continue
        mine = listing.seller_id == viewer.id
        bucket = None
        if not mine:
            if buckets is not None and listing.id in buckets:
                bucket = buckets[listing.id]
            elif viewer.home_cell_res8 is not None:
                bucket = cells.bucket_between_cells(viewer.home_cell_res8, listing.cell_res8)
        views.append(
            ListingOut(
                id=listing.id,
                seller=author_view(seller, assets, storage),
                title=listing.title,
                description=listing.description,
                category=listing.category,
                condition=listing.condition,
                pricing=listing.pricing,
                price_kes=listing.price_kes,
                images=[
                    PostImageOut(media_id=image.media_id, variants=variants)
                    for image in listing.images
                    if (variants := image_variants(assets.get(image.media_id), storage))
                ],
                area_label=labels[listing.cell_res9],
                distance_bucket=bucket,
                availability=listing.availability,
                is_expired=listing.is_expired(now),
                expires_at=listing.expires_at,
                sold_at=listing.sold_at,
                is_mine=mine,
                saved=listing.id in saved,
                my_conversation_id=chats.get(listing.id),
                status=listing.status,
                reach_estimate=listing.reach_estimate if mine else None,
                created_at=listing.created_at,
            )
        )
    return views
