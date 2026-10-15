"""Request and response models for groups.

Scanned by `tests/test_privacy_schemas.py` like every other response. A public
group is placed by an area label and a distance band — never a cell or a point —
and a private group by nothing at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.core.pagination import CursorPage
from app.modules.feed.schemas import AuthorOut, PostOut
from app.modules.groups.models import (
    GroupJoinPolicy,
    GroupMemberStatus,
    GroupRole,
    GroupVisibility,
)
from app.modules.location.cells import DistanceBucket
from app.modules.media.schemas import ImageVariantOut
from app.modules.users.schemas import AreaLabelOut


class GroupCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200, examples=["Kamakwa Estate Residents"])
    description: str | None = Field(default=None, max_length=10_000)
    category: str = Field(examples=["estate"])
    visibility: GroupVisibility = Field(
        description=(
            "`public` groups are discoverable nearby and readable by anyone signed in; "
            "`private` groups are invite-only and never discovered."
        )
    )
    join_policy: GroupJoinPolicy = Field(
        default=GroupJoinPolicy.open,
        description="For public groups: join at once, or ask and wait for an admin.",
    )
    requires_post_approval: bool = False
    rules: str | None = Field(default=None, max_length=20_000)
    cover_media_id: uuid.UUID | None = Field(
        default=None, description="A ready upload with purpose `group`."
    )


class GroupUpdateIn(BaseModel):
    """Only the fields sent are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    category: str | None = None
    visibility: GroupVisibility | None = Field(
        default=None,
        description=(
            "Making a group public places it at *your* home area, which is what it "
            "will then be discovered near. Making it private removes its place."
        ),
    )
    join_policy: GroupJoinPolicy | None = None
    requires_post_approval: bool | None = None
    rules: str | None = Field(default=None, max_length=20_000)
    cover_media_id: uuid.UUID | None = None


class MembershipOut(BaseModel):
    role: GroupRole
    status: GroupMemberStatus
    joined_at: datetime | None = None
    muted_until: datetime | None = None


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    category: str
    rules: str | None = None
    visibility: GroupVisibility
    join_policy: GroupJoinPolicy
    requires_post_approval: bool
    cover: dict[str, ImageVariantOut] | None = None
    member_count: int
    area_label: AreaLabelOut | None = Field(
        default=None, description="Public groups only. Private groups have no place."
    )
    distance_bucket: DistanceBucket | None = Field(
        default=None, description="Public groups only: how far from the caller's home, as a band."
    )
    my_membership: MembershipOut | None = Field(
        default=None, description="The caller's membership, invitation or request, if any."
    )
    chat_conversation_id: uuid.UUID | None = Field(
        default=None, description="The group's linked chat, shown to active members only."
    )
    created_at: datetime


GroupPageOut = CursorPage[GroupOut]


class DiscoveryOut(BaseModel):
    items: list[GroupOut]
    next_cursor: str | None = None
    radius_km: float = Field(description="How far this search reached, rounded to 0.5 km.")


class MemberOut(BaseModel):
    user: AuthorOut
    role: GroupRole
    status: GroupMemberStatus
    joined_at: datetime | None = None
    muted_until: datetime | None = Field(
        default=None, description="Shown to the group's moderators and above."
    )
    ban_reason: str | None = Field(default=None, description="Shown to the group's admins.")


MemberPageOut = CursorPage[MemberOut]


class InviteIn(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class InvitedOut(BaseModel):
    invited: list[uuid.UUID] = Field(
        description=(
            "Newly invited. People already in, or whose request was just approved, are not listed."
        )
    )


class InviteLinkCreateIn(BaseModel):
    expires_in_days: int | None = Field(default=None, ge=1, le=365)
    max_uses: int | None = Field(default=None, ge=1, le=10_000)


class InviteLinkOut(BaseModel):
    id: uuid.UUID
    expires_at: datetime
    max_uses: int | None = None
    use_count: int
    created_at: datetime
    token: str | None = Field(
        default=None,
        description=(
            "Only when the link is created. Only a hash is stored, so it cannot be "
            "shown again; share it as the app's invite URL."
        ),
    )


class RespondIn(BaseModel):
    action: Literal["accept", "decline"]


class ReviewIn(BaseModel):
    action: Literal["approve", "reject"]


class RoleIn(BaseModel):
    role: GroupRole = Field(
        description="`owner` hands the group over; you become an admin. Owner only."
    )


class MuteIn(BaseModel):
    until: datetime | None = Field(description="When the mute ends; null to unmute.")


class BanIn(BaseModel):
    reason: str | None = Field(
        default=None, max_length=500, description="Seen by the group's admins only."
    )


class PinIn(BaseModel):
    pinned: bool


class GroupPostPageOut(BaseModel):
    items: list[PostOut]
    pinned: list[PostOut] = Field(
        default_factory=list, description="Pinned posts, on the first page only."
    )
    next_cursor: str | None = None
