"""Request and response models for messaging.

Scanned by `tests/test_privacy_schemas.py` like every other response: people are
named by `AuthorOut`, which carries no location of any kind.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.pagination import CursorPage
from app.modules.feed.schemas import AuthorOut
from app.modules.media.schemas import ImageVariantOut
from app.modules.messaging.models import ConversationKind, MemberRole, MemberStatus, MessageKind
from app.modules.moderation.models import ReportReason

ConversationViewState = Literal["active", "request_sent", "request_received", "declined"]


class DirectOpenIn(BaseModel):
    user_id: uuid.UUID


class GroupCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200, examples=["Kamakwa estate committee"])
    member_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)


class GroupRenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MembersAddIn(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)


class RoleIn(BaseModel):
    role: MemberRole


class MessageCreateIn(BaseModel):
    body: str | None = Field(default=None, max_length=20_000, examples=["Niko njiani"])
    media_id: uuid.UUID | None = Field(
        default=None, description="A ready upload with purpose `message`. Instead of `body`."
    )
    reply_to_id: uuid.UUID | None = None


class ReadIn(BaseModel):
    seq: int = Field(ge=0, description="The highest sequence number the user has seen.")


class MuteIn(BaseModel):
    muted: bool


class RequestResponseIn(BaseModel):
    action: Literal["accept", "decline"]


class BlockAndReportIn(BaseModel):
    reason: ReportReason = Field(examples=["harassment"])
    details: str | None = Field(default=None, max_length=1000)


class ReplyRefOut(BaseModel):
    id: uuid.UUID
    seq: int


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    seq: int
    kind: MessageKind
    sender: AuthorOut | None = Field(default=None, description="Null for system messages.")
    body: str | None = Field(default=None, description="Null when deleted, or for an image.")
    image: dict[str, ImageVariantOut] | None = None
    reply_to: ReplyRefOut | None = None
    system: dict[str, Any] | None = Field(
        default=None,
        description=(
            "For system messages: `event` (created, members_added, member_joined, "
            "member_removed, member_left, renamed, role_changed) and the user IDs "
            "involved. Rendered by the client from the member list, so names are "
            "always current."
        ),
    )
    deleted: bool = False
    created_at: datetime


class MessagePageOut(BaseModel):
    items: list[MessageOut]
    has_more: bool


class MemberOut(BaseModel):
    user: AuthorOut
    role: MemberRole
    status: MemberStatus
    last_read_seq: int


class ConversationOut(BaseModel):
    id: uuid.UUID
    kind: ConversationKind
    title: str | None = None
    listing_id: uuid.UUID | None = Field(
        default=None, description="For a conversation about a listing."
    )
    state: ConversationViewState = Field(
        description=(
            "From the caller's side. A request the caller sent stays `request_sent` "
            "whatever the recipient did with it."
        )
    )
    my_role: MemberRole
    my_status: MemberStatus
    muted: bool
    other_user: AuthorOut | None = Field(
        default=None, description="Direct conversations: the other person."
    )
    other_last_read_seq: int | None = Field(
        default=None,
        description="Direct, accepted conversations: how far the other person has read.",
    )
    member_count: int
    last_seq: int
    last_read_seq: int
    unread_count: int
    last_message: MessageOut | None = None
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


ConversationPageOut = CursorPage[ConversationOut]


class SyncOut(BaseModel):
    conversations: list[ConversationOut]
    next_since: datetime = Field(description="Pass back as `since` on the next sync.")
    has_more: bool = Field(description="True when truncated: sync again immediately.")
