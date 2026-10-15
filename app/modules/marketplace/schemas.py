"""Request and response models for the marketplace.

A listing is placed by its area label and a distance band — the "approximate
pickup area" the brief asks for. The exact meeting point is agreed in chat, and
nothing here could locate the seller.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.pagination import CursorPage
from app.modules.feed.schemas import AuthorOut, PostImageOut
from app.modules.location.cells import DistanceBucket
from app.modules.marketplace.models import ListingAvailability, ListingCondition, ListingPricing
from app.modules.moderation.models import ContentStatus
from app.modules.users.schemas import AreaLabelOut


class ListingCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=300, examples=["Three-seater sofa, grey"])
    description: str | None = Field(default=None, max_length=20_000)
    category: str = Field(examples=["furniture"])
    condition: ListingCondition
    pricing: ListingPricing = Field(
        description="`fixed`, `negotiable`, or `free` (then `price_kes` is 0 or omitted)."
    )
    price_kes: int | None = Field(default=None, ge=0, description="Whole Kenyan shillings.")
    media_ids: list[uuid.UUID] = Field(
        default_factory=list, description="Ready `listing` uploads, in display order. At most 8."
    )


class ListingUpdateIn(BaseModel):
    """Only the fields sent are changed. `media_ids` replaces the photos."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    category: str | None = None
    condition: ListingCondition | None = None
    pricing: ListingPricing | None = None
    price_kes: int | None = Field(default=None, ge=0)
    media_ids: list[uuid.UUID] | None = None


class AvailabilityIn(BaseModel):
    availability: ListingAvailability
    buyer_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional, when marking it sold: who bought it. Must be someone who asked "
            "about this listing. Reviews (Phase 6) will be built on it."
        ),
    )


class ListingOut(BaseModel):
    id: uuid.UUID
    seller: AuthorOut
    title: str
    description: str
    category: str
    condition: ListingCondition
    pricing: ListingPricing
    price_kes: int
    images: list[PostImageOut]
    area_label: AreaLabelOut = Field(description="The approximate pickup area.")
    distance_bucket: DistanceBucket | None = Field(
        default=None, description="How far from the caller, as a band. Null on your own."
    )
    availability: ListingAvailability
    is_expired: bool
    expires_at: datetime
    sold_at: datetime | None = None
    is_mine: bool
    saved: bool
    my_conversation_id: uuid.UUID | None = Field(
        default=None, description="The caller's existing chat with the seller about this, if any."
    )
    status: ContentStatus = Field(
        description="Always `visible` to others; the seller also sees `hidden_pending_review`."
    )
    reach_estimate: int | None = Field(
        default=None,
        description="The seller's own listings only: about how many neighbours can see it.",
    )
    created_at: datetime


ListingPageOut = CursorPage[ListingOut]


class BrowseOut(BaseModel):
    items: list[ListingOut]
    next_cursor: str | None = None
    radius_km: float = Field(description="How far this search reached, rounded to 0.5 km.")
    sort: str


class BuyerOut(BaseModel):
    user: AuthorOut
    conversation_id: uuid.UUID
    accepted: bool = Field(description="Whether you have replied, accepting the conversation.")
