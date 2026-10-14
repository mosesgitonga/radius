"""Media assets.

One row per upload, tracking it from presigned URL to served variants. The row
exists before the bytes do, which is what makes the direct-to-storage flow
possible: the client gets an ID and a URL, uploads on its own, and tells us when
it is done.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin, db_enum


class MediaStatus(StrEnum):
    pending = "pending"
    """Presigned, not yet uploaded or not yet confirmed."""
    processing = "processing"
    ready = "ready"
    """Variants exist and the original has been discarded."""
    rejected = "rejected"
    """Not a usable image. The reason is stored and shown to the uploader."""


class MediaPurpose(StrEnum):
    """What an asset is for.

    Recorded at presign time rather than inferred later, because it decides the
    processing profile and the storage prefix — and because an avatar slot should
    not be fillable with something uploaded for a post.
    """

    avatar = "avatar"
    post = "post"
    story = "story"
    message = "message"
    """An image sent in a conversation."""
    listing = "listing"
    """Phase 5. Declared now so the enum does not need changing then."""
    event_cover = "event_cover"
    """Phase 7."""


_media_status = db_enum(MediaStatus, name="media_status", length=20)
_media_purpose = db_enum(MediaPurpose, name="media_purpose", length=20)


class MediaAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An uploaded image and the variants derived from it."""

    __tablename__ = "media_assets"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[MediaPurpose] = mapped_column(_media_purpose)
    status: Mapped[MediaStatus] = mapped_column(
        _media_status, default=MediaStatus.pending, server_default=MediaStatus.pending.value
    )

    declared_content_type: Mapped[str] = mapped_column(String(100))
    """What the client said it was uploading. A claim, kept for diagnosis only."""

    detected_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """What the bytes actually were, from magic bytes. This is the one that counts."""

    original_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Where the upload landed. **Nulled once processing succeeds**, because the
    original is deleted: it is the copy that still carries EXIF, and the brief
    requires discarding it."""

    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    variants: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    """`{"320": {"key": ..., "width": ..., "height": ..., "bytes": ...}, ...}`.

    JSONB rather than a child table: the set of sizes is configuration, the
    variants are always read together with the asset and never queried
    individually, and a join per image on a feed of twenty posts would be real
    cost for no benefit."""

    rejection_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="media_byte_size_non_negative"),
        # The sweeper looks for uploads that were presigned and then abandoned.
        Index("ix_media_assets_status_created_at", "status", "created_at"),
        Index("ix_media_assets_owner_id_purpose", "owner_id", "purpose"),
    )

    @property
    def is_ready(self) -> bool:
        return self.status == MediaStatus.ready

    def variant_keys(self) -> list[str]:
        return [str(entry["key"]) for entry in self.variants.values() if "key" in entry]

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<MediaAsset {self.id} {self.purpose}/{self.status}>"
