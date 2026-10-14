"""Request and response models for notifications."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.core.pagination import CursorPage
from app.modules.feed.schemas import AuthorOut
from app.modules.notifications.models import NotificationType


class DeviceIn(BaseModel):
    token: str = Field(min_length=1, max_length=4096, description="The FCM registration token.")
    platform: Literal["android", "ios", "web"]


class DeviceRemoveIn(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class PreferenceOut(BaseModel):
    type: NotificationType
    push_enabled: bool
    in_app_enabled: bool
    show_preview: bool = Field(
        description=(
            "Whether a push shows the message text. Off by default: phones are often "
            "shared, and a lock-screen preview is read by whoever picks it up."
        )
    )


class PreferenceIn(BaseModel):
    push_enabled: bool | None = None
    in_app_enabled: bool | None = None
    show_preview: bool | None = None


class NotificationOut(BaseModel):
    """IDs, not content. "Kamau and 3 others reacted to your post" is rendered by
    the client from the type, the actor, the counts and the target."""

    id: uuid.UUID
    type: str
    actor: AuthorOut | None = Field(default=None, description="The most recent actor.")
    other_actor_count: int = Field(description="How many others, for 'and 3 others'.")
    event_count: int
    target_type: str
    target_id: uuid.UUID
    data: dict[str, str]
    read: bool
    created_at: datetime
    updated_at: datetime


NotificationPageOut = CursorPage[NotificationOut]


class UnreadCountOut(BaseModel):
    count: int
