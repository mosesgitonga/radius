"""A person's circle: people they know, wherever either of them lives.

Two tables. A request is a proposal and a link is the fact. Keeping them apart
lets the request records be deleted on their own schedule (decision C.3) while
the links last as long as both people want them.

A link is stored once per pair, low ID first, so "are A and B linked?" is a
single unique lookup whichever way round it is asked.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin, db_enum


class CircleRequestStatus(StrEnum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    """Silent: to its sender the request still looks pending until it would have
    expired (decision C.2)."""
    cancelled = "cancelled"
    expired = "expired"


class CircleRequest(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "circle_requests"

    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[CircleRequestStatus] = mapped_column(
        db_enum(CircleRequestStatus, name="circle_request_status", length=16),
        default=CircleRequestStatus.pending,
        server_default=CircleRequestStatus.pending.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the recipient accepted or declined, or the request was cancelled."""

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the sender withdrew it. Set on a declined request too, without changing
    its status: the sender thought it was pending, and the decline still has to
    hold its cooldown."""

    __table_args__ = (
        CheckConstraint("requester_id <> recipient_id", name="circle_request_not_self"),
        # One open request per pair, whichever way round.
        Index(
            "uq_circle_requests_pending_pair",
            func.least(text("requester_id"), text("recipient_id")),
            func.greatest(text("requester_id"), text("recipient_id")),
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )


class CircleLink(Base):
    """Two people in each other's circle. The ordered pair is the primary key, so
    it is unique without a second index."""

    __tablename__ = "circle_links"

    user_low_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    user_high_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint("user_low_id < user_high_id", name="circle_link_ordered"),)

    @staticmethod
    def pair(first: uuid.UUID, second: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        """The pair in stored order. UUIDs compare as PostgreSQL compares them."""
        return (first, second) if first < second else (second, first)

    def other(self, user_id: uuid.UUID) -> uuid.UUID:
        return self.user_high_id if self.user_low_id == user_id else self.user_low_id
