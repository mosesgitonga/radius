"""Service posts — offering, seeking, hiring — and reviews.

**One table for three kinds.** A provider's offering, someone's need, and a job
advert share almost everything that matters — author, category, place, text,
moderation — and differ in a handful of optional fields. One table means one
search index, one radius query and one moderation path. The check constraints
hold each kind to its own fields.

**Reviews need a conversation.** A review is tied to a context — a listing or a
service post — and may only be written by someone who had an accepted
conversation with the other person about exactly that context. That rule is
enforced in the service; the table records which conversation qualified, so the
grounds for every review can be checked later.
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
    SmallInteger,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, db_enum
from app.modules.feed.models import LocatedContentMixin
from app.modules.moderation.models import ContentStatus


class ServiceKind(StrEnum):
    offering = "offering"
    """A provider's standing profile for one category."""
    seeking = "seeking"
    """Someone who needs a service."""
    hiring = "hiring"
    """A job advert. The kind job scams use, so the kind with the guard rails."""


class JobType(StrEnum):
    full_time = "full_time"
    part_time = "part_time"
    casual = "casual"
    contract = "contract"


class PayPeriod(StrEnum):
    hour = "hour"
    day = "day"
    week = "week"
    month = "month"
    one_off = "one_off"


class ReviewContext(StrEnum):
    listing = "listing"
    service_post = "service_post"


_kind = db_enum(ServiceKind, name="service_kind", length=16)
_job_type = db_enum(JobType, name="job_type", length=16)
_pay_period = db_enum(PayPeriod, name="pay_period", length=16)
_context = db_enum(ReviewContext, name="review_context", length=16)
_content_status = db_enum(ContentStatus, name="content_status", length=32)

SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(description, '')), 'B')"
)


class ServicePost(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, LocatedContentMixin):
    __tablename__ = "service_posts"

    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[ServiceKind] = mapped_column(_kind)
    category: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(String(20_000), default="", server_default="")

    # Offering
    rate_info: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    availability_info: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    travel_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """How far the provider will travel. Shown as a number the provider chose, which
    says nothing about where they live."""

    # Seeking
    budget_info: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Hiring
    job_type: Mapped[JobType | None] = mapped_column(_job_type, nullable=True)
    pay_min_kes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pay_max_kes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pay_period: Mapped[PayPeriod | None] = mapped_column(_pay_period, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Filled, or no longer needed. Closed posts leave search but stay readable."""

    status: Mapped[ContentStatus] = mapped_column(
        _content_status,
        default=ContentStatus.visible,
        server_default=ContentStatus.visible.value,
    )
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flagged_phrases: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    """Fee-demand phrases that held this post for review. What a moderator sees
    first, and what a held author is told."""

    coarse_centroid: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True)
    )

    __table_args__ = (
        CheckConstraint(
            "kind = 'hiring' OR (job_type IS NULL AND pay_min_kes IS NULL "
            "AND pay_max_kes IS NULL AND pay_period IS NULL)",
            name="service_post_pay_only_for_hiring",
        ),
        CheckConstraint(
            "kind = 'offering' OR (travel_km IS NULL AND rate_info IS NULL)",
            name="service_post_rates_only_for_offering",
        ),
        CheckConstraint(
            "kind <> 'offering' OR expires_at IS NULL", name="service_post_offering_no_expiry"
        ),
        CheckConstraint(
            "pay_min_kes IS NULL OR pay_max_kes IS NULL OR pay_min_kes <= pay_max_kes",
            name="service_post_pay_range_ordered",
        ),
        CheckConstraint(
            "travel_km IS NULL OR (travel_km >= 1 AND travel_km <= 200)",
            name="service_post_travel_in_range",
        ),
        CheckConstraint("length(title) > 0", name="service_post_title_not_empty"),
        Index("ix_service_posts_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_service_posts_kind_created_at_id", "kind", "created_at", "id"),
        # One live offering per provider per category: a provider updates their
        # plumbing profile, they do not post five of them.
        Index(
            "uq_service_posts_live_offering",
            "author_id",
            "category",
            unique=True,
            postgresql_where=text("kind = 'offering' AND deleted_at IS NULL"),
        ),
    )

    def is_open(self, now: datetime) -> bool:
        return self.closed_at is None and (self.expires_at is None or self.expires_at > now)


class Review(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "reviews"

    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    context_type: Mapped[ReviewContext] = mapped_column(_context)
    context_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    """The accepted conversation that made this review allowed."""

    rating: Mapped[int] = mapped_column(SmallInteger)
    body: Mapped[str] = mapped_column(String(10_000), default="", server_default="")
    reply_body: Mapped[str | None] = mapped_column(String(10_000), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[ContentStatus] = mapped_column(
        _content_status,
        default=ContentStatus.visible,
        server_default=ContentStatus.visible.value,
    )
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="review_rating_in_range"),
        CheckConstraint("reviewer_id <> subject_id", name="review_not_of_self"),
        Index("ix_reviews_subject_id_created_at", "subject_id", "created_at"),
        # One per pair per context. Partial, so a review deleted by its author can
        # be written again.
        Index(
            "uq_reviews_one_per_pair_per_context",
            "reviewer_id",
            "subject_id",
            "context_type",
            "context_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
