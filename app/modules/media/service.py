"""Media service: presigning, processing, and serving.

The flow and why it is shaped this way:

1. `create_upload` writes a `pending` row and returns a presigned PUT.
2. The client uploads straight to storage. **The API never sees image bytes** — a
   12 MB photo on a slow connection can take a minute, and a minute of a Uvicorn
   worker is a minute nobody else gets served.
3. `mark_uploaded` confirms and enqueues processing.
4. `process_asset` validates, strips metadata, renders WebP variants, and deletes
   the original.
5. Only variants are ever served.

The gap between steps 2 and 3 is unavoidable with direct upload: a client can
abandon an upload, leaving a `pending` row and possibly an orphaned object. The
sweeper handles that, which is cheaper than any scheme to prevent it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.providers.storage import (
    PresignedUpload,
    StorageError,
    StorageProvider,
    build_storage_key,
)
from app.modules.media.images import (
    ALLOWED_CONTENT_TYPES,
    OUTPUT_CONTENT_TYPE,
    OUTPUT_SUFFIX,
    ImageRejectedError,
    process_image,
)
from app.modules.media.models import MediaAsset, MediaPurpose, MediaStatus

logger = logging.getLogger(__name__)

# Suffix for the original, by declared type. Only used for the storage key, which
# is cosmetic — the real type comes from the bytes during processing.
_SUFFIXES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


@dataclass(frozen=True, slots=True)
class UploadTicket:
    asset: MediaAsset
    upload: PresignedUpload


@dataclass(frozen=True, slots=True)
class VariantView:
    """One served size. Carries a URL, never a storage key."""

    width: int
    height: int
    url: str


def variant_views(asset: MediaAsset, storage: StorageProvider) -> dict[str, VariantView]:
    """The asset's variants as client-facing URLs, smallest first."""
    views: dict[str, VariantView] = {}
    for size in sorted(asset.variants, key=lambda value: int(value)):
        entry: dict[str, Any] = asset.variants[size]
        views[size] = VariantView(
            width=int(entry["width"]),
            height=int(entry["height"]),
            url=storage.public_url(storage_key=str(entry["key"])),
        )
    return views


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
async def create_upload(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    owner_id: uuid.UUID,
    purpose: MediaPurpose,
    content_type: str,
    settings: Settings,
) -> UploadTicket:
    """Reserve an asset and presign its upload."""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise BadRequestError(
            "That image format is not supported",
            details={"supported": sorted(ALLOWED_CONTENT_TYPES)},
        )

    storage_key = build_storage_key(
        purpose=f"original/{purpose}",
        owner_id=owner_id,
        suffix=_SUFFIXES.get(content_type, ".bin"),
    )
    asset = MediaAsset(
        owner_id=owner_id,
        purpose=purpose,
        status=MediaStatus.pending,
        declared_content_type=content_type,
        original_key=storage_key,
    )
    session.add(asset)
    await session.flush()

    try:
        upload = storage.presign_upload(
            storage_key=storage_key,
            content_type=content_type,
            max_bytes=settings.media_max_upload_bytes,
        )
    except StorageError as exc:
        raise ConflictError("Uploads are temporarily unavailable") from exc

    logger.info(
        "media_upload_presigned",
        extra={"media_id": str(asset.id), "purpose": str(purpose)},
    )
    return UploadTicket(asset=asset, upload=upload)


async def get_asset(session: AsyncSession, asset_id: uuid.UUID) -> MediaAsset | None:
    return (
        await session.execute(select(MediaAsset).where(MediaAsset.id == asset_id))
    ).scalar_one_or_none()


async def require_own_asset(
    session: AsyncSession, *, asset_id: uuid.UUID, owner_id: uuid.UUID
) -> MediaAsset:
    """An asset the caller owns, or an error.

    404 for somebody else's asset rather than 403: media IDs are opaque, and
    confirming that one exists would let a caller enumerate them.
    """
    asset = await get_asset(session, asset_id)
    if asset is None or asset.owner_id != owner_id:
        raise NotFoundError("Media not found")
    return asset


async def mark_uploaded(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    asset: MediaAsset,
) -> MediaAsset:
    """Confirm an upload landed, and move the asset into processing.

    The object's existence is checked here rather than trusted, because a client
    that calls this without uploading would otherwise leave the asset stuck in
    `processing` until a human looked.
    """
    if asset.status == MediaStatus.ready:
        return asset
    if asset.status == MediaStatus.rejected:
        raise ConflictError(
            "That upload was rejected",
            details={"reason": asset.rejection_reason or "unknown"},
        )
    if asset.original_key is None:  # pragma: no cover — only if already processed
        raise ConflictError("That upload has already been processed")

    if not await storage.object_exists(storage_key=asset.original_key):
        raise BadRequestError("No upload was found for that media. Upload it first.")

    asset.status = MediaStatus.processing
    await session.flush()
    return asset


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
async def process_asset(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    asset: MediaAsset,
    settings: Settings,
) -> MediaAsset:
    """Validate, strip metadata, render variants, discard the original.

    This is where the brief's EXIF requirement is actually met. `process_image`
    decodes the pixels and writes new files, so nothing from the source's metadata
    can survive — and then the original, the only copy that still carries GPS, is
    deleted.

    A rejection is a normal outcome, not an exception: the asset records why, the
    uploader is told, and the orphaned object is removed.
    """
    if asset.original_key is None:
        raise ConflictError("That upload has already been processed")

    try:
        data = await storage.get_object(storage_key=asset.original_key)
    except StorageError:
        logger.error("media_original_missing", extra={"media_id": str(asset.id)})
        return await _reject(
            session,
            storage,
            asset=asset,
            code="upload_missing",
            reason="The uploaded file could not be read",
        )

    sizes = (
        (settings.media_avatar_size,)
        if asset.purpose == MediaPurpose.avatar
        else settings.media_webp_size_list
    )

    try:
        processed = process_image(
            data,
            sizes=sizes,
            quality=settings.media_webp_quality,
            max_bytes=settings.media_max_upload_bytes,
            max_pixels=settings.media_max_pixels,
            declared_content_type=asset.declared_content_type,
        )
    except ImageRejectedError as exc:
        logger.info("media_rejected", extra={"media_id": str(asset.id), "code": exc.code})
        return await _reject(session, storage, asset=asset, code=exc.code, reason=exc.reason)

    variants: dict[str, Any] = {}
    for size, variant in processed.variants.items():
        key = build_storage_key(
            purpose=f"public/{asset.purpose}", owner_id=asset.owner_id, suffix=OUTPUT_SUFFIX
        )
        await storage.put_object(
            storage_key=key, data=variant.data, content_type=OUTPUT_CONTENT_TYPE
        )
        variants[str(size)] = {
            "key": key,
            "width": variant.width,
            "height": variant.height,
            "bytes": len(variant.data),
        }

    # Only now that every variant is safely stored. Deleting first would risk
    # losing the image entirely if a write failed part-way.
    original_key = asset.original_key
    await storage.delete_object(storage_key=original_key)

    asset.status = MediaStatus.ready
    asset.detected_content_type = processed.detected_content_type
    asset.byte_size = len(data)
    asset.source_width = processed.source_width
    asset.source_height = processed.source_height
    asset.variants = variants
    # Nulled to record that the EXIF-bearing copy is gone.
    asset.original_key = None
    asset.processed_at = datetime.now(UTC)
    await session.flush()

    logger.info(
        "media_processed",
        extra={"media_id": str(asset.id), "variants": len(variants)},
    )
    return asset


async def _reject(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    asset: MediaAsset,
    code: str,
    reason: str,
) -> MediaAsset:
    if asset.original_key:
        await storage.delete_object(storage_key=asset.original_key)
    asset.status = MediaStatus.rejected
    asset.rejection_code = code
    asset.rejection_reason = reason
    asset.original_key = None
    asset.processed_at = datetime.now(UTC)
    await session.flush()
    return asset


# ---------------------------------------------------------------------------
# Attaching assets to content
# ---------------------------------------------------------------------------
async def claim_assets_for(
    session: AsyncSession,
    *,
    media_ids: list[uuid.UUID],
    owner_id: uuid.UUID,
    purpose: MediaPurpose,
) -> list[MediaAsset]:
    """Fetch assets a caller is about to attach to content, checking all of it.

    Four things have to hold, and each has a failure mode worth naming:

    * **Every ID exists** — otherwise a post silently loses an image.
    * **The caller owns them** — otherwise someone attaches another user's photo
      to their own post, which is both theft and a way to make somebody else's
      private image public.
    * **They are `ready`** — a pending asset has no variants, so the post would
      render blank.
    * **The purpose matches** — an asset presigned for an avatar must not become a
      post image, or the processing profile applied to it was the wrong one.

    Duplicates are rejected too; the database would refuse them anyway, and a
    clear message beats an integrity error.
    """
    if not media_ids:
        return []
    if len(set(media_ids)) != len(media_ids):
        raise BadRequestError("The same image cannot be attached twice")

    assets = list(
        (await session.execute(select(MediaAsset).where(MediaAsset.id.in_(media_ids))))
        .scalars()
        .all()
    )
    by_id = {asset.id: asset for asset in assets}

    missing = [media_id for media_id in media_ids if media_id not in by_id]
    if missing:
        raise NotFoundError(
            "Some images were not found", details={"media_ids": [str(m) for m in missing]}
        )

    for asset in assets:
        if asset.owner_id != owner_id:
            raise ForbiddenError("That image belongs to someone else")
        if asset.purpose != purpose:
            raise BadRequestError(
                "That image was uploaded for something else",
                details={"expected_purpose": str(purpose), "actual": str(asset.purpose)},
            )
        if asset.status != MediaStatus.ready:
            raise ConflictError(
                "That image is not ready yet",
                details={"media_id": str(asset.id), "status": str(asset.status)},
            )

    # Returned in the order asked for, so a post's images keep their order.
    return [by_id[media_id] for media_id in media_ids]


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
async def sweep_abandoned_uploads(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    settings: Settings,
    now: datetime | None = None,
    limit: int = 500,
) -> int:
    """Clean up uploads that were presigned and never completed.

    Direct-to-storage uploads make these inevitable: the client may lose its
    connection between the presign and the confirmation. Each one is a database
    row and possibly a stored object, so they are swept rather than tolerated.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=settings.media_pending_ttl_hours)
    stale = list(
        (
            await session.execute(
                select(MediaAsset)
                .where(
                    MediaAsset.status == MediaStatus.pending,
                    MediaAsset.created_at < cutoff,
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    for asset in stale:
        if asset.original_key:
            await storage.delete_object(storage_key=asset.original_key)
        await session.delete(asset)

    if stale:
        await session.flush()
        logger.info("abandoned_uploads_swept", extra={"count": len(stale)})
    return len(stale)


async def assets_awaiting_processing(
    session: AsyncSession, *, older_than: timedelta, limit: int = 20
) -> list[MediaAsset]:
    """Assets stuck in `processing`, for the catch-up sweep.

    Enqueueing is best-effort, so a job can be lost if Redis blinks between the
    client's confirmation and the push. This finds those uploads so they are
    processed a few minutes late rather than never. The age threshold keeps the
    sweep from racing a job that is merely still in the queue.
    """
    cutoff = datetime.now(UTC) - older_than
    return list(
        (
            await session.execute(
                select(MediaAsset)
                .where(
                    MediaAsset.status == MediaStatus.processing,
                    MediaAsset.updated_at < cutoff,
                )
                .order_by(MediaAsset.updated_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


def strip_asset(asset: MediaAsset, *, code: str, reason: str) -> list[str]:
    """Make an asset unservable, and return the storage keys to delete.

    The row stays, so whatever references it keeps a valid foreign key and a
    moderator can still see that an image existed. Deleting the objects is left to
    the caller, who knows whether that should wait for a commit.
    """
    keys = asset.variant_keys()
    if asset.original_key:
        keys.append(asset.original_key)
    asset.variants = {}
    asset.original_key = None
    asset.status = MediaStatus.rejected
    asset.rejection_code = code
    asset.rejection_reason = reason
    return keys


async def purge_owner_media(
    session: AsyncSession, storage: StorageProvider, *, owner_id: uuid.UUID
) -> int:
    """Delete every stored image belonging to a purged account.

    The images are the personal data, so the objects are deleted outright. The
    rows stay, marked `rejected` with code `purged` and no variants, because a
    soft-deleted post still references them and moderation may need to know an
    image existed. Nothing servable remains.
    """
    assets = list(
        (await session.execute(select(MediaAsset).where(MediaAsset.owner_id == owner_id)))
        .scalars()
        .all()
    )
    for asset in assets:
        for key in strip_asset(
            asset, code="purged", reason="The account that uploaded this was deleted"
        ):
            await storage.delete_object(storage_key=key)
    if assets:
        await session.flush()
    return len(assets)


async def export_owner_media(session: AsyncSession, *, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    """The user's uploads, for the data export.

    Lists what exists and its state, not URLs: an export file is something people
    forward and store, and a working link to every image in it would outlive the
    export's purpose. The images themselves are visible in the app.
    """
    assets = (
        await session.execute(
            select(MediaAsset)
            .where(MediaAsset.owner_id == owner_id)
            .order_by(MediaAsset.created_at)
        )
    ).scalars()
    return [
        {
            "id": str(asset.id),
            "purpose": str(asset.purpose),
            "status": str(asset.status),
            "width": asset.source_width,
            "height": asset.source_height,
            "created_at": asset.created_at.isoformat() if asset.created_at else None,
            "metadata_removed": asset.status == MediaStatus.ready,
        }
        for asset in assets
    ]
