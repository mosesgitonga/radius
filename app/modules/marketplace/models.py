"""Listings, their images, and saved listings.

**Two statuses, kept apart.** `availability` is the seller's: available, reserved,
sold. `status` is the platform's moderation state, named exactly as on posts so
the existing auto-hide rule applies to listings unchanged. A seller cannot mark
their way out of a moderation decision, and a moderator's hide does not pretend
the item was sold.

**Two locations, for two jobs.** `centroid` (from the shared mixin) is the seller's
home cell centre and drives the radius filter, as for posts. `coarse_centroid` is
the centre of the enclosing res-8 cell, and is what "nearest first" sorts by: the
order then carries no more precision than the distance band shown beside each
listing. Sorting by the fine centroid would let someone spoofing their position
watch listings swap places and narrow a seller's home down to a res-9 cell, which
the band alone does not allow.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, db_enum
from app.modules.feed.models import LocatedContentMixin
from app.modules.moderation.models import ContentStatus


class ListingCondition(StrEnum):
    new = "new"
    like_new = "like_new"
    good = "good"
    fair = "fair"
    for_parts = "for_parts"


class ListingPricing(StrEnum):
    fixed = "fixed"
    negotiable = "negotiable"
    free = "free"
    """`price_kes` is 0, so sorting by price needs no special case."""


class ListingAvailability(StrEnum):
    available = "available"
    reserved = "reserved"
    """Promised to someone. Still browsable, so a buyer sees it may come free."""
    sold = "sold"


_condition = db_enum(ListingCondition, name="listing_condition", length=16)
_pricing = db_enum(ListingPricing, name="listing_pricing", length=16)
_availability = db_enum(ListingAvailability, name="listing_availability", length=16)
_content_status = db_enum(ContentStatus, name="content_status", length=32)

# Weighted so a match in the title outranks one in the description. `simple`: no
# stemming, because listings mix English and Swahili and an English stemmer
# helps one while mangling the other.
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(description, '')), 'B')"
)


class Listing(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, LocatedContentMixin):
    __tablename__ = "listings"

    seller_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(String(20_000), default="", server_default="")
    category: Mapped[str] = mapped_column(String(50), index=True)
    condition: Mapped[ListingCondition] = mapped_column(_condition)

    pricing: Mapped[ListingPricing] = mapped_column(_pricing)
    price_kes: Mapped[int] = mapped_column(BigInteger)
    """Whole Kenyan shillings, as all money in the API."""

    availability: Mapped[ListingAvailability] = mapped_column(
        _availability,
        default=ListingAvailability.available,
        server_default=ListingAvailability.available.value,
    )
    sold_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    """The buyer, if the seller named one. Only someone who talked to the seller
    about this listing can be named. Phase 6 builds seller reviews on it."""
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expiry_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When the seller was told this listing is about to expire. Cleared on renewal,
    so the next term gets its own reminder."""

    status: Mapped[ContentStatus] = mapped_column(
        _content_status,
        default=ContentStatus.visible,
        server_default=ContentStatus.visible.value,
    )
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reach_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    """Of the normalised title and description, for duplicate detection."""

    coarse_centroid: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )
    """The res-8 cell centre. Sorting only — see the module docstring."""

    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True)
    )

    images: Mapped[list[ListingImage]] = relationship(
        back_populates="listing",
        cascade="all, delete-orphan",
        order_by="ListingImage.position",
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint("price_kes >= 0", name="listing_price_non_negative"),
        CheckConstraint("pricing <> 'free' OR price_kes = 0", name="listing_free_means_zero"),
        CheckConstraint(
            "availability <> 'sold' OR sold_at IS NOT NULL", name="listing_sold_has_time"
        ),
        CheckConstraint("length(title) > 0", name="listing_title_not_empty"),
        Index("ix_listings_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_listings_availability_created_at_id", "availability", "created_at", "id"),
        Index("ix_listings_seller_id_created_at", "seller_id", "created_at"),
        Index("ix_listings_price_kes_id", "price_kes", "id"),
    )

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now


class ListingImage(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "listing_images"

    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    media_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_assets.id", ondelete="RESTRICT"))
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    listing: Mapped[Listing] = relationship(back_populates="images")

    __table_args__ = (
        UniqueConstraint("listing_id", "position", name="uq_listing_images_listing_id_position"),
        UniqueConstraint("listing_id", "media_id", name="uq_listing_images_listing_id_media_id"),
        CheckConstraint("position >= 0", name="listing_image_position_non_negative"),
    )


class SavedListing(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "saved_listings"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "listing_id", name="uq_saved_listings_user_id_listing_id"),
        Index("ix_saved_listings_user_id_created_at", "user_id", "created_at"),
    )
