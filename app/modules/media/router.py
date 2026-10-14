"""Media endpoints: presign, confirm, inspect, and set an avatar."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.core.queue import JobQueue, get_queue
from app.modules.auth.dependencies import ActiveUser, CurrentUser, user_rate_limit
from app.modules.media import service as media_service
from app.modules.media.models import MediaAsset, MediaPurpose
from app.modules.media.schemas import (
    AvatarIn,
    MediaOut,
    UploadRequestIn,
    UploadTicketOut,
)
from app.modules.media.views import image_variants

router = APIRouter(tags=["media"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]


def media_view(asset: MediaAsset, storage: StorageProvider) -> MediaOut:
    return MediaOut(
        id=asset.id,
        purpose=asset.purpose,
        status=asset.status,
        variants=image_variants(asset, storage) or {},
        rejection_code=asset.rejection_code,
        rejection_reason=asset.rejection_reason,
        created_at=asset.created_at,
    )


@router.post(
    "/media/uploads",
    response_model=UploadTicketOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start an image upload",
    description=(
        "Reserves an asset and returns a presigned URL. The client uploads the file "
        "**directly to storage** — the API never handles image bytes — then calls "
        "`POST /v1/media/{media_id}/complete`.\n\n"
        "The `Content-Type` sent with the upload must match the one requested here; "
        "it is part of the signature."
    ),
    dependencies=[Depends(user_rate_limit(bucket="media_upload", limit=60, window_seconds=3600))],
)
async def create_upload(
    payload: UploadRequestIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> UploadTicketOut:
    ticket = await media_service.create_upload(
        session,
        storage,
        owner_id=user.id,
        purpose=payload.purpose,
        content_type=payload.content_type,
        settings=settings,
    )
    return UploadTicketOut(
        media_id=ticket.asset.id,
        upload_url=ticket.upload.url,
        upload_method=ticket.upload.method,
        upload_headers=ticket.upload.headers,
        expires_in_seconds=ticket.upload.expires_in_seconds,
        max_bytes=settings.media_max_upload_bytes,
    )


@router.post(
    "/media/{media_id}/complete",
    response_model=MediaOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Confirm an upload",
    description=(
        "Tells the server the upload finished. Processing happens in the background: "
        "the image is checked from its bytes (not its claimed type), **all metadata "
        "including GPS is stripped**, WebP sizes are generated, and the original is "
        "deleted. Poll `GET /v1/media/{media_id}` until `status` is `ready`.\n\n"
        "Safe to call twice."
    ),
)
async def complete_upload(
    media_id: uuid.UUID,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
    queue: QueueDep,
) -> MediaOut:
    asset = await media_service.require_own_asset(session, asset_id=media_id, owner_id=user.id)
    asset = await media_service.mark_uploaded(session, storage, asset=asset)
    if not asset.is_ready:
        # Best-effort: if the queue is down, the worker's catch-up sweep picks the
        # asset up from its `processing` status a few minutes later.
        await queue.enqueue("process_media", str(asset.id))
    return media_view(asset, storage)


@router.get(
    "/media/{media_id}",
    response_model=MediaOut,
    summary="An upload's status and variants",
    description=(
        "The caller's own uploads only. Once `ready`, `variants` holds WebP URLs at "
        "each configured width; a `rejected` upload says why."
    ),
)
async def get_media(
    media_id: uuid.UUID, user: CurrentUser, session: SessionDep, storage: StorageDep
) -> MediaOut:
    asset = await media_service.require_own_asset(session, asset_id=media_id, owner_id=user.id)
    return media_view(asset, storage)


@router.put(
    "/me/avatar",
    response_model=MediaOut | None,
    summary="Set or clear my avatar",
    description=(
        "Points the profile at a `ready` upload made with `purpose: avatar`. Send "
        "`null` to remove the avatar."
    ),
)
async def set_avatar(
    payload: AvatarIn,
    user: ActiveUser,
    session: SessionDep,
    storage: StorageDep,
) -> MediaOut | None:
    if payload.media_id is None:
        user.avatar_media_id = None
        await session.flush()
        return None
    (asset,) = await media_service.claim_assets_for(
        session, media_ids=[payload.media_id], owner_id=user.id, purpose=MediaPurpose.avatar
    )
    user.avatar_media_id = asset.id
    await session.flush()
    return media_view(asset, storage)
