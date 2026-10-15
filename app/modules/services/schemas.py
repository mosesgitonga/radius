"""Request and response models for services, jobs and reviews.

A post is placed by area label and distance band, like everything else. A
provider's `travel_km` is a number they chose about how far they will go, which
says nothing about where they are.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.pagination import CursorPage
from app.modules.feed.schemas import AuthorOut
from app.modules.location.cells import DistanceBucket
from app.modules.moderation.models import ContentStatus
from app.modules.services.models import JobType, PayPeriod, ReviewContext, ServiceKind
from app.modules.users.schemas import AreaLabelOut


class ServicePostIn(BaseModel):
    kind: ServiceKind
    category: str = Field(examples=["plumbing"])
    title: str = Field(min_length=1, max_length=300, examples=["Plumber, 10 years' experience"])
    description: str | None = Field(default=None, max_length=20_000)
    rate_info: str | None = Field(
        default=None, max_length=1000, description="Offering only.", examples=["KES 800 per visit"]
    )
    availability_info: str | None = Field(
        default=None, max_length=1000, examples=["Weekdays after 2pm"]
    )
    travel_km: int | None = Field(
        default=None, ge=1, le=200, description="Offering only: how far you will travel."
    )
    budget_info: str | None = Field(default=None, max_length=1000, description="Seeking only.")
    job_type: JobType | None = Field(default=None, description="Hiring only.")
    pay_min_kes: int | None = Field(default=None, ge=0, description="Hiring only.")
    pay_max_kes: int | None = Field(default=None, ge=0, description="Hiring only.")
    pay_period: PayPeriod | None = Field(default=None, description="Hiring only.")


class ServicePostUpdateIn(BaseModel):
    """Only the fields sent are changed. The kind cannot change."""

    category: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    rate_info: str | None = Field(default=None, max_length=1000)
    availability_info: str | None = Field(default=None, max_length=1000)
    travel_km: int | None = Field(default=None, ge=1, le=200)
    budget_info: str | None = Field(default=None, max_length=1000)
    job_type: JobType | None = None
    pay_min_kes: int | None = Field(default=None, ge=0)
    pay_max_kes: int | None = Field(default=None, ge=0)
    pay_period: PayPeriod | None = None


class OpenIn(BaseModel):
    open: bool


class ProviderOut(AuthorOut):
    rating_average: float | None = None
    rating_count: int = 0


class ServicePostOut(BaseModel):
    id: uuid.UUID
    author: ProviderOut
    kind: ServiceKind
    category: str
    title: str
    description: str
    rate_info: str | None = None
    availability_info: str | None = None
    travel_km: int | None = None
    budget_info: str | None = None
    job_type: JobType | None = None
    pay_min_kes: int | None = None
    pay_max_kes: int | None = None
    pay_period: PayPeriod | None = None
    area_label: AreaLabelOut
    distance_bucket: DistanceBucket | None = None
    is_open: bool
    expires_at: datetime | None = None
    closed_at: datetime | None = None
    is_mine: bool
    my_conversation_id: uuid.UUID | None = None
    safety_notice: str | None = Field(
        default=None, description="Set on job posts. Show it before the caller makes contact."
    )
    status: ContentStatus = Field(
        description=(
            "Always `visible` to others. The author also sees `hidden_pending_review` "
            "when a post is held."
        )
    )
    held_for: list[str] | None = Field(
        default=None,
        description=(
            "The author's own held post only: the phrases that held it, so they know why."
        ),
    )
    created_at: datetime


ServicePostPageOut = CursorPage[ServicePostOut]


class ServiceBrowseOut(BaseModel):
    items: list[ServicePostOut]
    next_cursor: str | None = None
    radius_km: float = Field(description="How far this search reached, rounded to 0.5 km.")
    sort: str


class ReviewIn(BaseModel):
    subject_id: uuid.UUID = Field(description="The person being reviewed.")
    context_type: ReviewContext
    context_id: uuid.UUID = Field(description="The listing or service post the review is about.")
    rating: int = Field(ge=1, le=5)
    body: str | None = Field(default=None, max_length=10_000)


class ReviewUpdateIn(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    body: str | None = Field(default=None, max_length=10_000)


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


class ReviewOut(BaseModel):
    id: uuid.UUID
    reviewer: AuthorOut
    subject_id: uuid.UUID
    context_type: ReviewContext
    context_id: uuid.UUID
    rating: int
    body: str
    reply_body: str | None = None
    replied_at: datetime | None = None
    status: ContentStatus
    created_at: datetime
    updated_at: datetime


class ReviewPageOut(CursorPage[ReviewOut]):
    rating_average: float | None = None
    rating_count: int = 0
