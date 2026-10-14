"""Request and response models for media."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.media.images import ALLOWED_CONTENT_TYPES
from app.modules.media.models import MediaPurpose, MediaStatus


class UploadRequestIn(BaseModel):
    purpose: MediaPurpose = Field(examples=["post"])
    content_type: str = Field(
        description=f"One of: {', '.join(sorted(ALLOWED_CONTENT_TYPES))}.",
        examples=["image/jpeg"],
    )


class UploadTicketOut(BaseModel):
    """Everything a client needs to upload one image itself.

    The client PUTs the file to `upload_url` with exactly the `upload_headers`
    given — the content type is part of the signature, so changing it makes the
    upload fail — and then calls `POST /v1/media/{media_id}/complete`.
    """

    media_id: uuid.UUID
    upload_url: str
    upload_method: str = "PUT"
    upload_headers: dict[str, str]
    expires_in_seconds: int
    max_bytes: int = Field(
        description=(
            "The size limit. A presigned PUT cannot enforce it, so the server "
            "checks after upload; it is sent here so the client can refuse early "
            "and not spend the user's data on a file that will be rejected."
        )
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "media_id": "0c6f2b1e-5d0b-4c43-9f43-2a8d0d1c9e10",
                "upload_url": "https://media.example/original/post/...?X-Amz-Signature=...",
                "upload_method": "PUT",
                "upload_headers": {"Content-Type": "image/jpeg"},
                "expires_in_seconds": 900,
                "max_bytes": 12582912,
            }
        }
    )


class ImageVariantOut(BaseModel):
    width: int
    height: int
    url: str


class MediaOut(BaseModel):
    """An asset's state, and its variants once ready.

    Never includes a storage key or the original's location: only processed,
    metadata-free variants are ever addressable.
    """

    id: uuid.UUID
    purpose: MediaPurpose
    status: MediaStatus
    variants: dict[str, ImageVariantOut] = Field(
        default_factory=dict,
        description="Keyed by longest-edge width. Empty until processing finishes.",
    )
    rejection_code: str | None = None
    rejection_reason: str | None = None
    created_at: datetime


class AvatarIn(BaseModel):
    media_id: uuid.UUID | None = Field(
        description="A `ready` avatar upload, or null to remove the avatar."
    )
