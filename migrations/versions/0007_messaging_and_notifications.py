"""Messaging and notifications

Six tables, the whole of Phase 3:

* `conversations` — direct, group, and (from Phase 5) listing chats. A unique
  `direct_key` guarantees one direct conversation per pair, even if both people tap
  "message" at once. `last_seq` is the per-conversation sequence counter.
* `conversation_members` — role, status, and `last_read_seq`, a read high-water mark.
* `messages` — unique on `(conversation_id, seq)`, so a sequence number cannot be
  reused and "messages since seq N" is an index range scan.
* `device_tokens`, `notification_preferences`, `notifications`.

Reviewed by hand: the direct-key and group-title check constraints, the message
kind constraint (a system message has no sender), `messages.media_id` as RESTRICT
so an image cannot vanish from under a message, that the downgrade drops
messages before conversations, and that every enum column is created with its
CHECK constraint (see 0006 for why that needed saying).

Revision ID: 0007
Revises: 0006
Created: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "direct",
                "group",
                "listing",
                name="conversation_kind",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "active",
                "request_pending",
                "request_declined",
                name="conversation_state",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("request_sender_id", sa.Uuid(), nullable=True),
        sa.Column("direct_key", sa.String(length=80), nullable=True),
        sa.Column("listing_id", sa.Uuid(), nullable=True),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind <> 'direct' OR direct_key IS NOT NULL",
            name=op.f("ck_conversations_conversation_direct_has_key"),
        ),
        sa.CheckConstraint(
            "kind <> 'group' OR title IS NOT NULL",
            name=op.f("ck_conversations_conversation_group_has_title"),
        ),
        sa.CheckConstraint(
            "kind = 'direct' OR direct_key IS NULL",
            name=op.f("ck_conversations_conversation_key_only_for_direct"),
        ),
        sa.CheckConstraint(
            "last_seq >= 0", name=op.f("ck_conversations_conversation_last_seq_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_conversations_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["request_sender_id"],
            ["users.id"],
            name=op.f("fk_conversations_request_sender_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("direct_key", name=op.f("uq_conversations_direct_key")),
    )
    op.create_index(op.f("ix_conversations_group_id"), "conversations", ["group_id"], unique=False)
    op.create_index(
        op.f("ix_conversations_listing_id"), "conversations", ["listing_id"], unique=False
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"], unique=False)
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=4096), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_device_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_tokens")),
        sa.UniqueConstraint("token", name=op.f("uq_device_tokens_token")),
    )
    op.create_index(op.f("ix_device_tokens_user_id"), "device_tokens", ["user_id"], unique=False)
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("push_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("in_app_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("show_preview", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint("user_id", "type", name="uq_notification_preferences_user_id_type"),
    )
    op.create_index(
        op.f("ix_notification_preferences_user_id"),
        "notification_preferences",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "actor_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("event_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("collapse_key", sa.String(length=200), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_notifications_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"], unique=False)
    op.create_index(
        "ix_notifications_user_id_collapse_key_read_at",
        "notifications",
        ["user_id", "collapse_key", "read_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_user_id_updated_at",
        "notifications",
        ["user_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "conversation_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "admin",
                "member",
                name="member_role",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="member",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "invited",
                "left",
                "removed",
                name="member_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("invited_by_id", sa.Uuid(), nullable=True),
        sa.Column("last_read_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("muted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_read_seq >= 0",
            name=op.f("ck_conversation_members_member_last_read_seq_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_members_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_id"],
            ["users.id"],
            name=op.f("fk_conversation_members_invited_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversation_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_members")),
        sa.UniqueConstraint(
            "conversation_id", "user_id", name="uq_conversation_members_conversation_id_user_id"
        ),
    )
    op.create_index(
        op.f("ix_conversation_members_conversation_id"),
        "conversation_members",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_members_user_id"), "conversation_members", ["user_id"], unique=False
    )
    op.create_index(
        "ix_conversation_members_user_id_status",
        "conversation_members",
        ["user_id", "status"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sender_id", sa.Uuid(), nullable=True),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "text",
                "image",
                "system",
                name="message_kind",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("body", sa.String(length=20000), nullable=True),
        sa.Column("media_id", sa.Uuid(), nullable=True),
        sa.Column("reply_to_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(kind = 'text' AND body IS NOT NULL) OR (kind = 'image' AND media_id IS NOT NULL) OR (kind = 'system' AND body IS NOT NULL AND sender_id IS NULL)",
            name=op.f("ck_messages_message_kind_matches_content"),
        ),
        sa.CheckConstraint("seq > 0", name=op.f("ck_messages_message_seq_positive")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_assets.id"],
            name=op.f("fk_messages_media_id_media_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reply_to_id"],
            ["messages.id"],
            name=op.f("fk_messages_reply_to_id_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["users.id"],
            name=op.f("fk_messages_sender_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_id_seq"),
    )
    op.create_index(op.f("ix_messages_deleted_at"), "messages", ["deleted_at"], unique=False)
    op.create_index(op.f("ix_messages_sender_id"), "messages", ["sender_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_sender_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_deleted_at"), table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversation_members_user_id_status", table_name="conversation_members")
    op.drop_index(op.f("ix_conversation_members_user_id"), table_name="conversation_members")
    op.drop_index(
        op.f("ix_conversation_members_conversation_id"), table_name="conversation_members"
    )
    op.drop_table("conversation_members")
    op.drop_index("ix_notifications_user_id_updated_at", table_name="notifications")
    op.drop_index("ix_notifications_user_id_collapse_key_read_at", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(
        op.f("ix_notification_preferences_user_id"), table_name="notification_preferences"
    )
    op.drop_table("notification_preferences")
    op.drop_index(op.f("ix_device_tokens_user_id"), table_name="device_tokens")
    op.drop_table("device_tokens")
    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.drop_index(op.f("ix_conversations_listing_id"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_group_id"), table_name="conversations")
    op.drop_table("conversations")
