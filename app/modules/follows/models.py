"""Following a service, wherever either side lives.

People are never followed: only entities, and the type is a closed list
(decision C.5). A service provider is followed through one of their offerings;
posts made as that offering reach its followers, and the person's own posts do
not. Business profiles join the list in Phase 10.

`entity_id` is not a foreign key because it points at a different table for each
type. The retention run deletes follows whose entity no longer exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin, db_enum


class FollowEntityType(StrEnum):
    service_provider = "service_provider"
    """An offering service post. `business` follows in Phase 10."""


class Follow(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "follows"

    follower_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    entity_type: Mapped[FollowEntityType] = mapped_column(
        db_enum(FollowEntityType, name="follow_entity_type", length=32)
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "follower_id", "entity_type", "entity_id", name="uq_follows_follower_entity"
        ),
        # Follower counts.
        Index("ix_follows_entity", "entity_type", "entity_id"),
    )
