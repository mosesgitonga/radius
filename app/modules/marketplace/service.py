"""The marketplace: listing, finding, asking, and selling.

The anti-abuse rules sit at creation, where they are cheapest. A prohibited term
refuses the listing outright, and names the term so an honest seller can
rephrase. The same seller listing the same thing twice, while the first is still
live, gets the first one back with a 409: renewing is how a listing is brought
back to the top, not reposting.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import Select, and_, case, delete, func, literal, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, ConflictError, InvalidCursorError
from app.core.pagination import PageParams, decode_cursor, encode_cursor, paginate
from app.modules.feed.service import estimate_reach
from app.modules.location import cells
from app.modules.location.radius import RadiusEngine
from app.modules.location.service import (
    Anchor,
    AnchorMode,
    home_location_columns,
    resolve_anchor,
)
from app.modules.marketplace import access
from app.modules.marketplace.models import (
    Listing,
    ListingAvailability,
    ListingCondition,
    ListingImage,
    ListingPricing,
    SavedListing,
)
from app.modules.media.models import MediaPurpose
from app.modules.media.service import claim_assets_for
from app.modules.messaging import service as messaging_service
from app.modules.messaging.models import Conversation, ConversationKind
from app.modules.moderation.models import ContentStatus
from app.modules.users.models import User
from app.modules.users.service import blocked_user_ids

logger = logging.getLogger(__name__)

ListingSort = Literal["relevance", "newest", "nearest", "price_asc", "price_desc"]
MAX_SEARCH_RESULTS = 50
MAX_QUERY_TERMS = 8
BROWSABLE = (ListingAvailability.available, ListingAvailability.reserved)

_NON_WORD = re.compile(r"[^0-9a-zÀ-ɏ]+")
_QUERY_TERM = re.compile(r"[0-9a-zÀ-ɏ]+")


# ---------------------------------------------------------------------------
# Text rules
# ---------------------------------------------------------------------------
def normalise(text: str) -> str:
    """Lower case, punctuation to spaces, spaces collapsed. "iPhone 11, 64GB!!" and
    "iphone 11 64gb" are the same listing."""
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def content_hash(title: str, description: str) -> str:
    return hashlib.sha256(f"{normalise(title)}\n{normalise(description)}".encode()).hexdigest()


def prohibited_term(text: str, terms: tuple[str, ...]) -> str | None:
    """The first prohibited term appearing as a whole word or phrase, if any."""
    haystack = f" {normalise(text)} "
    for term in terms:
        needle = normalise(term)
        if needle and f" {needle} " in haystack:
            return term
    return None


def _clean(value: str | None, *, field: str, max_length: int, required: bool) -> str:
    cleaned = (value or "").strip()
    if required and not cleaned:
        raise BadRequestError(f"A listing needs a {field}")
    if len(cleaned) > max_length:
        raise BadRequestError(
            f"That {field} is too long", details={"field": field, "max_length": max_length}
        )
    return cleaned


def _check_price(pricing: ListingPricing, price_kes: int | None, settings: Settings) -> int:
    if pricing == ListingPricing.free:
        if price_kes not in (None, 0):
            raise BadRequestError("A free listing has no price")
        return 0
    if price_kes is None or price_kes < 1:
        raise BadRequestError("Give a price in shillings, or mark it free")
    if price_kes > settings.listing_max_price_kes:
        raise BadRequestError(
            "That price is too high", details={"max_price_kes": settings.listing_max_price_kes}
        )
    return price_kes


def _check_category(category: str, settings: Settings) -> str:
    if category not in settings.listing_category_list:
        raise BadRequestError(
            "That category does not exist",
            details={"categories": list(settings.listing_category_list)},
        )
    return category


def _check_allowed(title: str, description: str, settings: Settings) -> None:
    term = prohibited_term(f"{title} {description}", settings.prohibited_term_list)
    if term is not None:
        raise BadRequestError(
            "This item cannot be listed on MyRadius",
            details={"reason": "prohibited_item", "term": term},
        )


async def _check_not_duplicate(
    session: AsyncSession,
    *,
    seller_id: uuid.UUID,
    digest: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    statement = select(Listing.id).where(
        Listing.seller_id == seller_id,
        Listing.content_hash == digest,
        Listing.deleted_at.is_(None),
        Listing.availability != ListingAvailability.sold,
        Listing.expires_at > datetime.now(UTC),
    )
    if exclude_id is not None:
        statement = statement.where(Listing.id != exclude_id)
    existing = await session.scalar(statement.limit(1))
    if existing is not None:
        raise ConflictError(
            "You already have this listing up. Renew or edit it instead.",
            details={"reason": "duplicate", "listing_id": str(existing)},
        )


async def _images(
    session: AsyncSession, *, seller: User, media_ids: list[uuid.UUID], settings: Settings
) -> list[ListingImage]:
    if len(media_ids) > settings.listing_max_images:
        raise BadRequestError(
            f"A listing can have at most {settings.listing_max_images} photos",
            details={"max_images": settings.listing_max_images},
        )
    assets = await claim_assets_for(
        session, media_ids=media_ids, owner_id=seller.id, purpose=MediaPurpose.listing
    )
    return [ListingImage(media_id=asset.id, position=index) for index, asset in enumerate(assets)]


def _coarse_wkt(cell_res8: int) -> str:
    latitude, longitude = cells.cell_centroid(cell_res8)
    return f"SRID=4326;POINT({longitude} {latitude})"


# ---------------------------------------------------------------------------
# Listing and changing
# ---------------------------------------------------------------------------
async def create_listing(
    session: AsyncSession,
    redis: Redis,
    *,
    seller: User,
    title: str,
    description: str | None,
    category: str,
    condition: ListingCondition,
    pricing: ListingPricing,
    price_kes: int | None,
    media_ids: list[uuid.UUID],
    settings: Settings,
) -> tuple[Listing, int]:
    """List an item at the seller's home area, and estimate who will see it."""
    location = home_location_columns(seller)
    if location is None:
        raise ConflictError(
            "Set your home area before listing something",
            details={"action": "PUT /v1/me/location"},
        )
    cleaned_title = _clean(
        title, field="title", max_length=settings.listing_title_max_length, required=True
    )
    cleaned_description = _clean(
        description,
        field="description",
        max_length=settings.listing_description_max_length,
        required=False,
    )
    _check_allowed(cleaned_title, cleaned_description, settings)
    digest = content_hash(cleaned_title, cleaned_description)
    await _check_not_duplicate(session, seller_id=seller.id, digest=digest)

    listing = Listing(
        seller_id=seller.id,
        title=cleaned_title,
        description=cleaned_description,
        category=_check_category(category, settings),
        condition=condition,
        pricing=pricing,
        price_kes=_check_price(pricing, price_kes, settings),
        expires_at=datetime.now(UTC) + timedelta(days=settings.listing_ttl_days),
        content_hash=digest,
        coarse_centroid=_coarse_wkt(int(location["cell_res8"])),  # type: ignore[call-overload]
        **location,
    )
    listing.images = await _images(session, seller=seller, media_ids=media_ids, settings=settings)
    session.add(listing)
    await session.flush()

    reach = await estimate_reach(
        session,
        redis,
        cell_res8=listing.cell_res8,
        settings=settings,
        feature=RadiusFeature.marketplace,
    )
    listing.reach_estimate = reach
    await session.flush()
    logger.info("listing_created", extra={"category": category, "images": len(media_ids)})
    return listing, reach


async def update_listing(
    session: AsyncSession,
    *,
    seller: User,
    listing_id: uuid.UUID,
    changes: dict[str, Any],
    settings: Settings,
) -> Listing:
    """Edit a listing. The same checks as creating it: an edit is not a way past the
    prohibited list."""
    listing = await access.own_listing(session, listing_id=listing_id, seller_id=seller.id)
    if listing.availability == ListingAvailability.sold:
        raise ConflictError("A sold listing cannot be edited")
    if "title" in changes:
        listing.title = _clean(
            changes["title"],
            field="title",
            max_length=settings.listing_title_max_length,
            required=True,
        )
    if "description" in changes:
        listing.description = _clean(
            changes["description"],
            field="description",
            max_length=settings.listing_description_max_length,
            required=False,
        )
    if "category" in changes:
        listing.category = _check_category(changes["category"], settings)
    if "condition" in changes:
        listing.condition = ListingCondition(changes["condition"])
    if "pricing" in changes or "price_kes" in changes:
        pricing = ListingPricing(changes.get("pricing", listing.pricing))
        price = changes.get(
            "price_kes", None if pricing == ListingPricing.free else listing.price_kes
        )
        listing.pricing = pricing
        listing.price_kes = _check_price(pricing, price, settings)
    if "media_ids" in changes:
        listing.images = await _images(
            session, seller=seller, media_ids=list(changes["media_ids"] or []), settings=settings
        )
    _check_allowed(listing.title, listing.description, settings)
    digest = content_hash(listing.title, listing.description)
    await _check_not_duplicate(session, seller_id=seller.id, digest=digest, exclude_id=listing.id)
    listing.content_hash = digest
    await session.flush()
    await session.refresh(listing)
    return listing


async def delete_listing(session: AsyncSession, *, seller: User, listing_id: uuid.UUID) -> None:
    listing = await access.own_listing(session, listing_id=listing_id, seller_id=seller.id)
    listing.deleted_at = datetime.now(UTC)
    await session.flush()


async def renew_listing(
    session: AsyncSession, *, seller: User, listing_id: uuid.UUID, settings: Settings
) -> Listing:
    """Another full term from now. Allowed until a grace period after expiry; past
    that, the item is listed afresh."""
    listing = await access.own_listing(session, listing_id=listing_id, seller_id=seller.id)
    now = datetime.now(UTC)
    if listing.availability == ListingAvailability.sold:
        raise ConflictError("A sold listing cannot be renewed")
    if listing.expires_at + timedelta(days=settings.listing_renewal_grace_days) < now:
        raise ConflictError(
            "This listing expired too long ago to renew; list it again",
            details={"reason": "renewal_window_passed"},
        )
    listing.expires_at = now + timedelta(days=settings.listing_ttl_days)
    listing.renewed_at = now
    await session.flush()
    await session.refresh(listing)
    return listing


async def set_availability(
    session: AsyncSession,
    *,
    seller: User,
    listing_id: uuid.UUID,
    availability: ListingAvailability,
    buyer_id: uuid.UUID | None,
) -> Listing:
    """Mark a listing available, reserved, or sold.

    Naming the buyer is optional, and only someone who talked to the seller about
    this listing can be named — so a sale on record always had a conversation
    behind it, which is what Phase 6's reviews will require.
    """
    listing = await access.own_listing(session, listing_id=listing_id, seller_id=seller.id)
    if buyer_id is not None and availability != ListingAvailability.sold:
        raise BadRequestError("A buyer can only be named when marking it sold")
    if buyer_id is not None:
        buyers = {
            buyer
            for buyer, _conversation, _state in await messaging_service.listing_buyers(
                session, listing_id=listing.id, seller_id=seller.id
            )
        }
        if buyer_id not in buyers:
            raise BadRequestError(
                "Only someone who asked about this listing can be named as the buyer"
            )

    listing.availability = availability
    if availability == ListingAvailability.sold:
        listing.sold_at = listing.sold_at or datetime.now(UTC)
        listing.sold_to_id = buyer_id
    else:
        listing.sold_at = None
        listing.sold_to_id = None
    await session.flush()
    await session.refresh(listing)
    return listing


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
async def get_listing(session: AsyncSession, *, viewer: User, listing_id: uuid.UUID) -> Listing:
    return await access.visible_listing(session, listing_id=listing_id, viewer_id=viewer.id)


async def my_listings(
    session: AsyncSession,
    *,
    seller: User,
    availability: ListingAvailability | None,
    params: PageParams,
) -> tuple[list[Listing], str | None]:
    statement = select(Listing).where(
        Listing.seller_id == seller.id,
        Listing.deleted_at.is_(None),
        Listing.status != ContentStatus.removed,
    )
    if availability is not None:
        statement = statement.where(Listing.availability == availability)
    return await paginate(
        session, statement, sort_column=Listing.created_at, id_column=Listing.id, params=params
    )


@dataclass(frozen=True, slots=True)
class BrowsePage:
    listings: list[Listing]
    buckets: dict[uuid.UUID, cells.DistanceBucket]
    next_cursor: str | None
    radius_km: float
    anchor: Anchor


def _ts_query(query: str) -> str | None:
    """Prefix-match every word: "sof cush" finds "sofa cushions". Built from
    letters and digits only, so nothing a user types reaches the parser as syntax."""
    terms = _QUERY_TERM.findall(query.lower())[:MAX_QUERY_TERMS]
    if not terms:
        return None
    return " & ".join(f"{term}:*" for term in terms)


def _bucket_expression(anchor: Anchor) -> Any:
    """The distance band as an ordinal, from the listing's coarse cell to the
    viewer's coarse cell — the same cells, and the same boundaries, as the band
    shown. Spherical, to agree with the band computed from H3."""
    distance = func.ST_Distance(
        Listing.coarse_centroid,
        func.ST_GeogFromText(_coarse_wkt(anchor.cell_res8)),
        False,
    )
    return case(
        *((distance < bound, index) for index, (bound, _bucket) in enumerate(cells.BUCKET_BOUNDS)),
        else_=len(cells.BUCKET_BOUNDS),
    )


def _encode_nearest(bucket: int, created_at: datetime, listing_id: uuid.UUID) -> str:
    payload = json.dumps([bucket, created_at.isoformat(), str(listing_id)]).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_nearest(cursor: str) -> tuple[int, datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        bucket, created, listing_id = json.loads(base64.urlsafe_b64decode(padded))
        return int(bucket), datetime.fromisoformat(created), uuid.UUID(listing_id)
    except (ValueError, TypeError) as exc:
        raise InvalidCursorError from exc


async def browse(
    session: AsyncSession,
    redis: Redis,
    *,
    viewer: User,
    query: str | None,
    category: str | None,
    condition: ListingCondition | None,
    min_price: int | None,
    max_price: int | None,
    sort: ListingSort | None,
    distance_km: float | None,
    mode: AnchorMode,
    params: PageParams,
    settings: Settings,
) -> BrowsePage:
    """Listings for sale near the viewer.

    Within the marketplace radius, or the viewer's own filter up to its cap.
    Available and reserved, unexpired, visible, and from nobody blocked either way.
    """
    anchor = await resolve_anchor(redis, user=viewer, mode=mode)
    if anchor is None:
        raise ConflictError(
            "Set your home area to see what is for sale nearby",
            details={"action": "PUT /v1/me/location"},
        )
    engine = RadiusEngine(session, redis, settings)
    radius = await engine.radius_for(RadiusFeature.marketplace, anchor.cell_res8)
    radius_km = engine.effective_radius_km(radius, distance_km)
    if min_price is not None and max_price is not None and min_price > max_price:
        raise BadRequestError("The minimum price is above the maximum")

    bucket = _bucket_expression(anchor).label("bucket")
    statement: Select[Any] = select(Listing, bucket).where(
        Listing.deleted_at.is_(None),
        Listing.status == ContentStatus.visible,
        Listing.availability.in_(BROWSABLE),
        Listing.expires_at > datetime.now(UTC),
        func.ST_DWithin(
            Listing.centroid, func.ST_GeogFromText(anchor.centroid_wkt), int(radius_km * 1000)
        ),
    )
    hidden = await blocked_user_ids(session, viewer.id)
    if hidden:
        statement = statement.where(Listing.seller_id.not_in(hidden))
    if category is not None:
        statement = statement.where(Listing.category == _check_category(category, settings))
    if condition is not None:
        statement = statement.where(Listing.condition == condition)
    if min_price is not None:
        statement = statement.where(Listing.price_kes >= min_price)
    if max_price is not None:
        statement = statement.where(Listing.price_kes <= max_price)

    ts_query = _ts_query(query) if query else None
    search = func.to_tsquery("simple", literal(ts_query)) if ts_query else None
    if search is not None:
        statement = statement.where(Listing.search_vector.op("@@")(search))
    chosen: ListingSort = sort or ("relevance" if search is not None else "newest")
    if chosen == "relevance" and search is None:
        chosen = "newest"

    limit = params.limit
    next_cursor: str | None = None
    if chosen == "relevance":
        assert search is not None  # noqa: S101 — narrowed above
        rows = (
            await session.execute(
                statement.order_by(
                    func.ts_rank(Listing.search_vector, search).desc(),
                    Listing.created_at.desc(),
                    Listing.id.desc(),
                ).limit(min(limit, MAX_SEARCH_RESULTS))
            )
        ).all()
    elif chosen == "nearest":
        epoch = func.extract("epoch", Listing.created_at)
        if params.cursor:
            last_bucket, last_created, last_id = _decode_nearest(params.cursor)
            statement = statement.where(
                or_(
                    bucket > last_bucket,
                    and_(
                        bucket == last_bucket,
                        tuple_(Listing.created_at, Listing.id) < tuple_(last_created, last_id),
                    ),
                )
            )
        rows = (
            await session.execute(
                statement.order_by(bucket.asc(), epoch.desc(), Listing.id.desc()).limit(limit + 1)
            )
        ).all()
        if len(rows) > limit:
            rows = rows[:limit]
            last, last_bucket_value = rows[-1]
            next_cursor = _encode_nearest(int(last_bucket_value), last.created_at, last.id)
    else:
        sort_column = Listing.created_at if chosen == "newest" else Listing.price_kes
        descending = chosen != "price_asc"
        if params.cursor:
            value, row_id = decode_cursor(params.cursor)
            keyset = tuple_(sort_column, Listing.id)
            anchor_row = tuple_(literal(value), literal(row_id))
            statement = statement.where(keyset < anchor_row if descending else keyset > anchor_row)
        ordering = (
            (sort_column.desc(), Listing.id.desc())
            if descending
            else (sort_column.asc(), Listing.id.asc())
        )
        rows = (await session.execute(statement.order_by(*ordering).limit(limit + 1))).all()
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1][0]
            next_cursor = encode_cursor(getattr(last, sort_column.key), last.id)

    listings = [row[0] for row in rows]
    buckets = {row[0].id: cells.BUCKET_ORDER[int(row[1])] for row in rows}
    return BrowsePage(
        listings=listings,
        buckets=buckets,
        next_cursor=next_cursor,
        radius_km=radius_km,
        anchor=anchor,
    )


# ---------------------------------------------------------------------------
# Saving and asking
# ---------------------------------------------------------------------------
async def save_listing(session: AsyncSession, *, viewer: User, listing_id: uuid.UUID) -> None:
    """Idempotent."""
    listing = await access.visible_listing(session, listing_id=listing_id, viewer_id=viewer.id)
    try:
        async with session.begin_nested():
            session.add(SavedListing(user_id=viewer.id, listing_id=listing.id))
    except IntegrityError:
        return


async def unsave_listing(session: AsyncSession, *, viewer: User, listing_id: uuid.UUID) -> None:
    await execute_dml(
        session,
        delete(SavedListing).where(
            SavedListing.user_id == viewer.id, SavedListing.listing_id == listing_id
        ),
    )


async def saved_listings(
    session: AsyncSession, *, viewer: User, params: PageParams
) -> tuple[list[Listing], str | None]:
    """Most recently saved first. Listings since deleted, removed, or from someone
    now blocked drop out; sold and expired ones stay, marked as such."""
    hidden = await blocked_user_ids(session, viewer.id)
    statement = (
        select(SavedListing)
        .join(Listing, Listing.id == SavedListing.listing_id)
        .where(
            SavedListing.user_id == viewer.id,
            Listing.deleted_at.is_(None),
            Listing.status == ContentStatus.visible,
        )
    )
    if hidden:
        statement = statement.where(Listing.seller_id.not_in(hidden))
    saved, next_cursor = await paginate(
        session,
        statement,
        sort_column=SavedListing.created_at,
        id_column=SavedListing.id,
        params=params,
    )
    by_id = {
        listing.id: listing
        for listing in (
            await session.execute(
                select(Listing).where(Listing.id.in_([row.listing_id for row in saved]))
            )
        ).scalars()
    }
    return [by_id[row.listing_id] for row in saved if row.listing_id in by_id], next_cursor


async def open_chat(session: AsyncSession, *, buyer: User, listing_id: uuid.UUID) -> Conversation:
    """Chat with the seller. Only about something still for sale: a sold or
    expired listing can be read, but not asked about."""
    listing = await access.visible_listing(session, listing_id=listing_id, viewer_id=buyer.id)
    if listing.availability == ListingAvailability.sold or listing.is_expired(datetime.now(UTC)):
        existing = await session.scalar(
            select(Conversation).where(
                Conversation.kind == ConversationKind.listing,
                Conversation.listing_id == listing.id,
                Conversation.created_by_id == buyer.id,
            )
        )
        if existing is None:
            raise ConflictError("This listing is no longer for sale")
        return existing
    return await messaging_service.open_listing_chat(
        session, buyer=buyer, seller_id=listing.seller_id, listing_id=listing.id
    )


async def listing_buyers(
    session: AsyncSession, *, seller: User, listing_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    listing = await access.own_listing(session, listing_id=listing_id, seller_id=seller.id)
    hidden = await blocked_user_ids(session, seller.id)
    return [
        (buyer, conversation_id, str(state))
        for buyer, conversation_id, state in await messaging_service.listing_buyers(
            session, listing_id=listing.id, seller_id=seller.id
        )
        if buyer not in hidden
    ]


async def my_conversations_about(
    session: AsyncSession, *, viewer: User, listing_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """The viewer's own conversation about each listing, for "continue chat"."""
    if not listing_ids:
        return {}
    rows = await session.execute(
        select(Conversation.listing_id, Conversation.id).where(
            Conversation.kind == ConversationKind.listing,
            Conversation.listing_id.in_(listing_ids),
            Conversation.created_by_id == viewer.id,
        )
    )
    return {listing_id: conversation_id for listing_id, conversation_id in rows if listing_id}


async def saved_ids(
    session: AsyncSession, *, viewer: User, listing_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    if not listing_ids:
        return set()
    return set(
        (
            await session.execute(
                select(SavedListing.listing_id).where(
                    SavedListing.user_id == viewer.id, SavedListing.listing_id.in_(listing_ids)
                )
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def purge_user_listings(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, int]:
    """Soft-delete a purged account's listings — their images go with the media
    purge — and forget what they saved."""
    return {
        "listings": await execute_dml(
            session,
            update(Listing)
            .where(Listing.seller_id == user_id, Listing.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC)),
        ),
        "saved_listings": await execute_dml(
            session, delete(SavedListing).where(SavedListing.user_id == user_id)
        ),
    }


async def export_user_listings(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    listings = (
        await session.execute(
            select(Listing).where(Listing.seller_id == user_id).order_by(Listing.created_at)
        )
    ).scalars()
    saved = (
        await session.execute(
            select(SavedListing.listing_id, SavedListing.created_at)
            .where(SavedListing.user_id == user_id)
            .order_by(SavedListing.created_at)
        )
    ).all()

    def _when(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "listings": [
            {
                "id": str(listing.id),
                "title": listing.title,
                "description": listing.description,
                "category": listing.category,
                "condition": str(listing.condition),
                "pricing": str(listing.pricing),
                "price_kes": listing.price_kes,
                "availability": str(listing.availability),
                "image_media_ids": [str(image.media_id) for image in listing.images],
                "created_at": _when(listing.created_at),
                "expires_at": _when(listing.expires_at),
                "sold_at": _when(listing.sold_at),
                "deleted_at": _when(listing.deleted_at),
            }
            for listing in listings
        ],
        "saved_listings": [
            {"listing_id": str(listing_id), "saved_at": _when(saved_at)}
            for listing_id, saved_at in saved
        ],
    }
