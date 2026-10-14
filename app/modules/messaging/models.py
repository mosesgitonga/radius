"""Conversations, members, and messages.

**Ordering is a per-conversation sequence, allocated under a row lock.** A client
reconnecting after a dropped connection asks for "messages since seq N", and for
that to be safe, sequence order must equal commit order. Neither timestamps nor a
global sequence give that: a global value is handed out at insert time, so a
message numbered 42 can commit before 41, and a client that synced in between
would skip 41 forever. Incrementing `conversations.last_seq` to allocate a message's
number takes that row's lock and holds it to commit, so within a conversation, the
next number cannot be handed out until the previous message is durable.

The cost is that sends in one conversation are serialised. That is the right trade
for chat: two people typing in one thread are not a throughput problem, and the
alternative is a sync bug that loses messages silently.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, db_enum


class ConversationKind(StrEnum):
    direct = "direct"
    group = "group"
    listing = "listing"
    """Buyer and seller, from Phase 5. The kind exists now so the data model does
    not need changing then, as the brief asks."""


class ConversationState(StrEnum):
    active = "active"
    request_pending = "request_pending"
    """A first message from someone with no accepted conversation. The sender may
    send one message; the recipient decides."""
    request_declined = "request_declined"
    """Declined. The sender cannot send again or reopen it — which is what makes
    declining mean something."""


class MemberRole(StrEnum):
    owner = "owner"
    admin = "admin"
    member = "member"


class MemberStatus(StrEnum):
    active = "active"
    invited = "invited"
    """Added to a group chat by someone the user has no accepted conversation with.
    They see the invitation, not the messages, until they accept — otherwise a
    stranger could add anyone to a chat and message them through it, which is the
    message-request rule with a hole in it."""
    left = "left"
    removed = "removed"


class MessageKind(StrEnum):
    text = "text"
    image = "image"
    system = "system"
    """Server-written: "Wanjiku added Kamau", "Kamau left"."""


_kind = db_enum(ConversationKind, name="conversation_kind", length=20)
_state = db_enum(ConversationState, name="conversation_state", length=24)
_role = db_enum(MemberRole, name="member_role", length=16)
_member_status = db_enum(MemberStatus, name="member_status", length=16)
_message_kind = db_enum(MessageKind, name="message_kind", length=16)


def direct_key_for(first: uuid.UUID, second: uuid.UUID) -> str:
    """The canonical key for a pair, order-independent.

    A unique index on this is what guarantees two people only ever have one direct
    conversation — even if both tap "message" at the same moment.
    """
    low, high = sorted((str(first), str(second)))
    return f"{low}:{high}"


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "conversations"

    kind: Mapped[ConversationKind] = mapped_column(_kind)
    state: Mapped[ConversationState] = mapped_column(
        _state, default=ConversationState.active, server_default=ConversationState.active.value
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    request_sender_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    """Who started a message request. Kept after acceptance, since "who reached out
    first" is useful context for a moderator looking at a harassment report."""

    direct_key: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)

    listing_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    """Phase 5. Nullable and not yet a foreign key, as the brief asks."""
    group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    """Phase 4: a group's linked chat."""

    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind <> 'direct' OR direct_key IS NOT NULL", name="conversation_direct_has_key"
        ),
        CheckConstraint(
            "kind = 'direct' OR direct_key IS NULL", name="conversation_key_only_for_direct"
        ),
        CheckConstraint(
            "kind <> 'group' OR title IS NOT NULL", name="conversation_group_has_title"
        ),
        CheckConstraint("last_seq >= 0", name="conversation_last_seq_non_negative"),
        # The sync endpoint: conversations changed since a cursor.
        Index("ix_conversations_updated_at", "updated_at"),
    )


class ConversationMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "conversation_members"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MemberRole] = mapped_column(
        _role, default=MemberRole.member, server_default=MemberRole.member.value
    )
    status: Mapped[MemberStatus] = mapped_column(
        _member_status, default=MemberStatus.active, server_default=MemberStatus.active.value
    )
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    last_read_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    """Read state is a high-water mark, not a row per message. One integer per
    member answers both "how many unread" and "seen up to here", and marking read
    is one UPDATE however many messages arrived."""

    visible_from_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    """Messages at or below this sequence number are not shown to this member.

    Set to the conversation's `last_seq` when someone joins a group chat, so a
    person added later does not read what was said before they arrived — the
    members who said it had no idea they would be an audience.
    """

    muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "user_id", name="uq_conversation_members_conversation_id_user_id"
        ),
        # A user's inbox: their active memberships.
        Index("ix_conversation_members_user_id_status", "user_id", "status"),
        CheckConstraint("last_read_seq >= 0", name="member_last_read_seq_non_negative"),
    )


class Message(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    sender_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    """Null only for system messages."""

    seq: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[MessageKind] = mapped_column(_message_kind)
    body: Mapped[str | None] = mapped_column(String(20_000), nullable=True)
    media_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=True
    )
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # Fetching by sequence, and the guarantee that numbers are not reused.
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_id_seq"),
        CheckConstraint("seq > 0", name="message_seq_positive"),
        # Deleting a message clears its content, so the content rules hold only
        # while it is live. System messages cannot be deleted.
        CheckConstraint(
            "(kind = 'text' AND (body IS NOT NULL OR deleted_at IS NOT NULL))"
            " OR (kind = 'image' AND (media_id IS NOT NULL OR deleted_at IS NOT NULL))"
            " OR (kind = 'system' AND body IS NOT NULL AND sender_id IS NULL)",
            name="message_kind_matches_content",
        ),
    )
