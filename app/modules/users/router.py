"""User endpoints: profile, consent, blocking, and data rights."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.errors import ForbiddenError, NotFoundError
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.core.redis import get_redis
from app.modules.auth import service as auth_service
from app.modules.auth.dependencies import ActiveUser, CurrentUser, user_rate_limit
from app.modules.feed import service as feed_service
from app.modules.location import cells
from app.modules.location import service as location_service
from app.modules.media import service as media_service
from app.modules.media.schemas import ImageVariantOut
from app.modules.media.views import image_variants
from app.modules.messaging import service as messaging_service
from app.modules.notifications import service as notifications_service
from app.modules.users import service as users_service
from app.modules.users.models import Block, User
from app.modules.users.schemas import (
    AreaLabelOut,
    BlockCreateIn,
    BlockOut,
    ConsentOut,
    ConsentUpdateIn,
    DeletionStatusOut,
    MeOut,
    ProfileUpdateIn,
    PublicProfileOut,
)

router = APIRouter(tags=["users"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]


async def _avatar(
    session: AsyncSession, storage: StorageProvider, user: User
) -> dict[str, ImageVariantOut] | None:
    if user.avatar_media_id is None:
        return None
    asset = await media_service.get_asset(session, user.avatar_media_id)
    return image_variants(asset, storage)


async def _area_label(
    session: AsyncSession, redis: Redis, user: User, settings: Settings
) -> AreaLabelOut | None:
    if not user.has_home_location:
        return None
    label = await location_service.area_label_for_cells(
        session, redis, user=user, settings=settings
    )
    return AreaLabelOut(label=label.label, precision=label.precision)


def _next_home_change_allowed_at(user: User, settings: Settings):  # type: ignore[no-untyped-def]
    if user.home_changed_at is None or settings.home_change_cooldown_days <= 0:
        return None
    return user.home_changed_at + timedelta(days=settings.home_change_cooldown_days)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@router.get(
    "/me",
    response_model=MeOut,
    tags=["users"],
    summary="My profile",
    description=(
        "The caller's own account. Includes their own phone number; contains no "
        "coordinates, because none are stored."
    ),
)
async def get_me(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> MeOut:
    return MeOut(
        id=user.id,
        display_name=user.display_name,
        phone_number=user.phone_number,
        status=user.status,
        member_since=user.created_at,
        area_label=await _area_label(session, redis, user, settings),
        has_home_location=user.has_home_location,
        home_set_at=user.home_set_at,
        next_home_change_allowed_at=_next_home_change_allowed_at(user, settings),
        is_verified_neighbour=user.is_verified_neighbour,
        rating_average=float(user.rating_average) if user.rating_average else None,
        rating_count=user.rating_count,
        deletion_requested_at=user.deletion_requested_at,
        avatar_media_id=user.avatar_media_id,
        avatar=await _avatar(session, storage, user),
    )


@router.patch(
    "/me",
    response_model=MeOut,
    summary="Update my profile",
    description=(
        "Changes the caller's display name. Returns the full profile, so the "
        "client needs no follow-up request.\n\n"
        "Audited as a field name only, never the new value: a display name is "
        "personal data and the audit log is kept for two years."
    ),
    dependencies=[Depends(user_rate_limit(bucket="profile_update", limit=10, window_seconds=3600))],
)
async def update_me(
    payload: ProfileUpdateIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> MeOut:
    await users_service.update_profile(session, user=user, display_name=payload.display_name)
    return await get_me(user, session, redis, settings, storage)


@router.get(
    "/users/{user_id}",
    response_model=PublicProfileOut,
    summary="A neighbour's public profile",
    description=(
        "Shows an area label and a distance band, never a position. Returns 404 "
        "if either party has blocked the other, so a block is not discoverable by "
        "probing."
    ),
)
async def get_public_profile(
    user_id: uuid.UUID,
    viewer: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> PublicProfileOut:
    target = await users_service.get_user(session, user_id)
    if target is None:
        raise NotFoundError("User not found")

    # 404 rather than 403: a distinct "you are blocked" response would let
    # somebody confirm a block, and work around it by asking a friend to look.
    if await users_service.is_blocked_between(session, viewer.id, target.id):
        raise NotFoundError("User not found")

    bucket = None
    if viewer.home_cell_res8 is not None and target.home_cell_res8 is not None:
        # Computed cell to cell, not point to point, so repeating the measurement
        # from different positions cannot sharpen it.
        bucket = cells.bucket_between_cells(viewer.home_cell_res8, target.home_cell_res8)

    return PublicProfileOut(
        id=target.id,
        display_name=target.display_name,
        avatar=await _avatar(session, storage, target),
        area_label=await _area_label(session, redis, target, settings),
        member_since=target.created_at,
        distance_bucket=bucket,
        is_verified_neighbour=target.is_verified_neighbour,
        rating_average=float(target.rating_average) if target.rating_average else None,
        rating_count=target.rating_count,
    )


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
@router.get(
    "/me/consents",
    response_model=list[ConsentOut],
    summary="My consent record",
    description="The current decision for each consent type, with its document version.",
)
async def list_consents(user: CurrentUser, session: SessionDep) -> list[ConsentOut]:
    consents = await users_service.current_consents(session, user.id)
    return [
        ConsentOut(
            consent_type=consent_type,
            granted=consent.granted,
            document_version=consent.document_version,
            recorded_at=consent.created_at,
        )
        for consent_type, consent in sorted(consents.items())
    ]


@router.put(
    "/me/consents",
    response_model=ConsentOut,
    summary="Change an optional consent",
    description=(
        "Grants or withdraws marketing consent. Required consents cannot be "
        "withdrawn while the account exists; deleting the account is the way to "
        "withdraw them, and the response says so."
    ),
)
async def set_consent(
    payload: ConsentUpdateIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ConsentOut:
    consent = await users_service.set_consent(
        session,
        user=user,
        consent_type=payload.consent_type,
        granted=payload.granted,
        settings=settings,
    )
    await session.flush()
    return ConsentOut(
        consent_type=consent.consent_type,
        granted=consent.granted,
        document_version=consent.document_version,
        recorded_at=consent.created_at,
    )


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------
@router.get(
    "/me/blocks",
    response_model=list[BlockOut],
    summary="Who I have blocked",
    description=(
        "People the caller blocked. Does not list people who blocked the caller: "
        "that would tell them something they have no business knowing."
    ),
)
async def list_blocks(user: CurrentUser, session: SessionDep) -> list[BlockOut]:
    statement = (
        select(Block.blocked_id, Block.created_at, User.display_name)
        .join(User, User.id == Block.blocked_id)
        .where(Block.blocker_id == user.id)
        .order_by(Block.created_at.desc())
    )
    rows = (await session.execute(statement)).all()
    return [
        BlockOut(user_id=blocked_id, display_name=display_name, blocked_at=created_at)
        for blocked_id, created_at, display_name in rows
    ]


@router.post(
    "/me/blocks",
    response_model=BlockOut,
    status_code=status.HTTP_201_CREATED,
    summary="Block a neighbour",
    description=(
        "Hides each user from the other everywhere in the product. Idempotent: "
        "blocking someone already blocked succeeds."
    ),
    dependencies=[Depends(user_rate_limit(bucket="block_user", limit=60, window_seconds=3600))],
)
async def block_user(payload: BlockCreateIn, user: CurrentUser, session: SessionDep) -> BlockOut:
    block = await users_service.block_user(session, blocker=user, blocked_id=payload.user_id)
    await session.flush()
    target = await users_service.require_user(session, payload.user_id)
    return BlockOut(
        user_id=block.blocked_id,
        display_name=target.display_name,
        blocked_at=block.created_at,
    )


@router.delete(
    "/me/blocks/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unblock a neighbour",
    description=(
        "Makes the two users visible to each other again. Returns 404 if that "
        "user was not blocked, so a client can tell a no-op from a success."
    ),
)
async def unblock_user(user_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await users_service.unblock_user(session, blocker=user, blocked_id=user_id)


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
@router.get(
    "/me/export",
    summary="Export my data",
    description=(
        "Everything held about the account, as JSON — the Data Protection Act's "
        "right of access. States explicitly what is *not* held, because 'we keep "
        "no location history' is a more useful answer than silence."
    ),
    response_model=dict,
    dependencies=[Depends(user_rate_limit(bucket="data_export", limit=5, window_seconds=86_400))],
)
async def export_my_data(
    user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    export = await users_service.export_user_data(session, user=user, settings=settings)
    # Each module exports its own data; the router assembles the whole. The users
    # service cannot do it itself, because the feed imports the users service.
    export["content"] = await feed_service.export_author_content(session, user_id=user.id)
    export["media"] = await media_service.export_owner_media(session, owner_id=user.id)
    export["messaging"] = await messaging_service.export_user_messages(session, user_id=user.id)
    export["notifications"] = await notifications_service.export_user_notifications(
        session, user_id=user.id
    )
    export["not_held"] = [
        item for item in export["not_held"] if item != "message content (no messaging yet)"
    ] + [
        "image metadata (EXIF, including GPS, is removed from every upload)",
        "who you were online with, or when: presence is held in memory for a minute",
    ]
    return export


@router.post(
    "/me/deletion-request",
    response_model=DeletionStatusOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request account deletion",
    description=(
        "Starts a grace period, after which the account is irreversibly purged. "
        "All sessions are revoked immediately. The request can be cancelled by "
        "signing in again before the period ends."
    ),
)
async def request_deletion(
    user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> DeletionStatusOut:
    outcome = await users_service.request_deletion(session, user=user, settings=settings)
    # Sessions go immediately: someone deleting their account has usually decided
    # they do not want it reachable, and leaving tokens live for thirty days
    # would not honour that.
    await auth_service.revoke_sessions_for_deleted_account(session, user_id=user.id)
    return DeletionStatusOut(requested_at=outcome.requested_at, purge_after=outcome.purge_after)


@router.delete(
    "/me/deletion-request",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel account deletion",
    description="Available until the grace period ends.",
)
async def cancel_deletion(user: CurrentUser, session: SessionDep) -> None:
    if user.deletion_requested_at is None:
        raise ForbiddenError("No deletion request is pending")
    await users_service.cancel_deletion(session, user=user)
