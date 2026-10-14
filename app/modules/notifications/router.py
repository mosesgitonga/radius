"""Notification endpoints: devices, preferences, and the in-app list."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionDep
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.modules.auth.dependencies import CurrentUser, current_session_id, user_rate_limit
from app.modules.feed.views import author_view, load_assets, load_users
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import Notification, NotificationType
from app.modules.notifications.schemas import (
    DeviceIn,
    DeviceRemoveIn,
    NotificationOut,
    NotificationPageOut,
    PreferenceIn,
    PreferenceOut,
    UnreadCountOut,
)
from app.modules.notifications.service import Preference

router = APIRouter(prefix="/notifications", tags=["notifications"])

StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]


def _preference_out(preference: Preference) -> PreferenceOut:
    return PreferenceOut(
        type=preference.type,
        push_enabled=preference.push_enabled,
        in_app_enabled=preference.in_app_enabled,
        show_preview=preference.show_preview,
    )


@router.post(
    "/devices",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Register this device for push",
    description=(
        "Call on every launch and whenever FCM issues a new token. The token is tied "
        "to the current sign-in: signing out of this device stops its pushes at once. "
        "A token registered by another account moves to this one."
    ),
    dependencies=[
        Depends(user_rate_limit(bucket="register_device", limit=30, window_seconds=3600))
    ],
)
async def register_device(
    payload: DeviceIn, request: Request, user: CurrentUser, session: SessionDep
) -> None:
    await notifications_service.register_device(
        session,
        user=user,
        device_session_id=current_session_id(request),
        token=payload.token,
        platform=payload.platform,
    )


@router.post(
    "/devices/remove",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop pushes to a device",
    description="A POST with a body, not a DELETE: the token is too long for a path.",
)
async def unregister_device(
    payload: DeviceRemoveIn, user: CurrentUser, session: SessionDep
) -> None:
    await notifications_service.unregister_device(session, user=user, token=payload.token)


@router.get(
    "/preferences",
    response_model=list[PreferenceOut],
    summary="Notification preferences",
    description="Every type, with defaults filled in.",
)
async def get_preferences(user: CurrentUser, session: SessionDep) -> list[PreferenceOut]:
    preferences = await notifications_service.preferences_for(session, user.id)
    return [_preference_out(preference) for _key, preference in sorted(preferences.items())]


@router.put(
    "/preferences/{kind}",
    response_model=PreferenceOut,
    summary="Change a notification preference",
    description="Omitted fields keep their current value.",
)
async def set_preference(
    kind: NotificationType, payload: PreferenceIn, user: CurrentUser, session: SessionDep
) -> PreferenceOut:
    preference = await notifications_service.set_preference(
        session,
        user=user,
        kind=kind,
        push_enabled=payload.push_enabled,
        in_app_enabled=payload.in_app_enabled,
        show_preview=payload.show_preview,
    )
    return _preference_out(preference)


@router.get(
    "",
    response_model=NotificationPageOut,
    summary="In-app notifications",
    description=(
        "Most recent activity first. Bursts are collapsed: repeated reactions to one "
        "post are one notification with a count, which moves back to the top as it "
        "grows. Messages have no entries here — the inbox is their list."
    ),
)
async def list_notifications(
    user: CurrentUser, session: SessionDep, storage: StorageDep, page: PageDep
) -> NotificationPageOut:
    rows, next_cursor = await notifications_service.list_notifications(
        session, user=user, params=page
    )
    return NotificationPageOut(items=await _views(session, storage, rows), next_cursor=next_cursor)


async def _views(
    session: AsyncSession, storage: StorageProvider, rows: list[Notification]
) -> list[NotificationOut]:
    users = await load_users(session, (row.actor_id for row in rows if row.actor_id))
    assets = await load_assets(session, [user.avatar_media_id for user in users.values()])
    views: list[NotificationOut] = []
    for row in rows:
        actor = users.get(row.actor_id) if row.actor_id else None
        views.append(
            NotificationOut(
                id=row.id,
                type=row.type,
                actor=author_view(actor, assets, storage) if actor else None,
                other_actor_count=max(len(row.actor_ids) - 1, 0),
                event_count=row.event_count,
                target_type=row.target_type,
                target_id=row.target_id,
                data={str(key): str(value) for key, value in row.data.items()},
                read=row.read_at is not None,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return views


@router.get(
    "/unread-count",
    response_model=UnreadCountOut,
    summary="Unread notifications",
    description="For the badge. A collapsed burst counts once.",
)
async def unread_count(user: CurrentUser, session: SessionDep) -> UnreadCountOut:
    return UnreadCountOut(count=await notifications_service.unread_count(session, user=user))


@router.post(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark one notification read",
    description="Reading does not move it in the list, which is ordered by activity.",
)
async def mark_read(notification_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await notifications_service.mark_read(session, user=user, notification_id=notification_id)


@router.post(
    "/read-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark every notification read",
    description="Idempotent. Later activity on the same thing starts a new notification.",
)
async def mark_all_read(user: CurrentUser, session: SessionDep) -> None:
    await notifications_service.mark_read(session, user=user)
