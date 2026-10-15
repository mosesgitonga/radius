"""Groups

Three tables — `groups`, `group_members`, `group_invite_links` — and the changes
group posts and linked chats need:

* `posts.group_id` and `conversations.group_id` become real foreign keys
  (CASCADE: a deleted group takes its posts and chat with it).
* `posts.approval_status` and `posts.pinned_at`, each allowed only on group posts.
* `media_assets.purpose` gains `group`; `audit_events.action` gains the seven group
  admin actions. Both are literal lists, as in 0006, so this migration means the
  same thing after the enums grow.

Reviewed by hand: the public-has-location and private-has-no-location checks on
`groups`, the ban-has-a-time check on `group_members`, the trigram GIN index on the
group name, and that autogenerate's duplicate copies of each enum constraint (as in
0007) are removed — `sa.Enum(create_constraint=True)` emits one already.

Revision ID: 0009
Revises: 0008
Created: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MEDIA_PURPOSES_BEFORE = ("avatar", "post", "story", "message", "listing", "event_cover")
MEDIA_PURPOSES_AFTER = ("avatar", "post", "story", "message", "group", "listing", "event_cover")

AUDIT_ACTIONS_BEFORE = (
    "account_created",
    "account_deletion_requested",
    "account_deletion_cancelled",
    "account_purged",
    "data_exported",
    "consent_granted",
    "consent_withdrawn",
    "profile_updated",
    "sessions_revoked",
    "refresh_token_reuse_detected",
    "home_location_set",
    "home_location_changed",
    "location_spoof_suspected",
    "user_blocked",
    "user_unblocked",
    "report_submitted",
    "content_auto_hidden",
)
AUDIT_ACTIONS_AFTER = (
    *AUDIT_ACTIONS_BEFORE,
    "group_deleted",
    "group_member_removed",
    "group_member_banned",
    "group_member_unbanned",
    "group_role_changed",
    "group_ownership_transferred",
    "group_post_removed",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_check(table: str, suffix: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{suffix}"), table, type_="check")
    op.create_check_constraint(suffix, table, condition)


def upgrade() -> None:
    op.create_geospatial_table(
        "groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=10000), server_default="", nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("rules", sa.String(length=20000), nullable=True),
        sa.Column(
            "visibility",
            sa.Enum(
                "public",
                "private",
                name="group_visibility",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "join_policy",
            sa.Enum(
                "open",
                "approval",
                name="group_join_policy",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column(
            "requires_post_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("cover_media_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("member_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chat_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("cell_res9", sa.BigInteger(), nullable=True),
        sa.Column("cell_res8", sa.BigInteger(), nullable=True),
        sa.Column("cell_res7", sa.BigInteger(), nullable=True),
        sa.Column("cell_res6", sa.BigInteger(), nullable=True),
        sa.Column(
            "centroid",
            Geography(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeogFromText",
                name="geography",
            ),
            nullable=True,
        ),
        sa.Column("area_id", sa.Uuid(), nullable=True),
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
            "visibility <> 'private' OR (centroid IS NULL AND cell_res9 IS NULL)",
            name=op.f("ck_groups_group_private_has_no_location"),
        ),
        sa.CheckConstraint(
            "visibility <> 'public' OR (centroid IS NOT NULL AND cell_res9 IS NOT NULL)",
            name=op.f("ck_groups_group_public_has_location"),
        ),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_groups_group_name_not_empty")),
        sa.CheckConstraint(
            "member_count >= 0", name=op.f("ck_groups_group_member_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["area_id"], ["areas.id"], name=op.f("fk_groups_area_id_areas"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["cover_media_id"],
            ["media_assets.id"],
            name=op.f("fk_groups_cover_media_id_media_assets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_groups_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_groups_owner_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
        sa.UniqueConstraint("chat_conversation_id", name=op.f("uq_groups_chat_conversation_id")),
    )
    op.create_geospatial_index(
        "idx_groups_centroid",
        "groups",
        ["centroid"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(op.f("ix_groups_category"), "groups", ["category"], unique=False)
    op.create_index(op.f("ix_groups_cell_res8"), "groups", ["cell_res8"], unique=False)
    op.create_index(op.f("ix_groups_deleted_at"), "groups", ["deleted_at"], unique=False)
    op.create_index(
        "ix_groups_name_trgm",
        "groups",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(op.f("ix_groups_owner_id"), "groups", ["owner_id"], unique=False)
    op.create_index(
        "ix_groups_visibility_member_count", "groups", ["visibility", "member_count"], unique=False
    )
    op.create_table(
        "group_invite_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("use_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "max_uses IS NULL OR max_uses > 0",
            name=op.f("ck_group_invite_links_group_invite_link_max_uses_positive"),
        ),
        sa.CheckConstraint(
            "use_count >= 0",
            name=op.f("ck_group_invite_links_group_invite_link_use_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_group_invite_links_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_group_invite_links_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_invite_links")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_group_invite_links_token_hash")),
    )
    op.create_index(
        op.f("ix_group_invite_links_group_id"), "group_invite_links", ["group_id"], unique=False
    )
    op.create_table(
        "group_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "admin",
                "moderator",
                "member",
                name="group_role",
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
                "pending",
                "invited",
                "banned",
                name="group_member_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("invited_by_id", sa.Uuid(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("banned_by_id", sa.Uuid(), nullable=True),
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ban_reason", sa.String(length=500), nullable=True),
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
            "status <> 'banned' OR banned_at IS NOT NULL",
            name=op.f("ck_group_members_group_member_ban_has_time"),
        ),
        sa.ForeignKeyConstraint(
            ["banned_by_id"],
            ["users.id"],
            name=op.f("fk_group_members_banned_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_group_members_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_id"],
            ["users.id"],
            name=op.f("fk_group_members_invited_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_group_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_members")),
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_members_group_id_user_id"),
    )
    op.create_index(
        "ix_group_members_group_id_status_role",
        "group_members",
        ["group_id", "status", "role"],
        unique=False,
    )
    op.create_index(
        "ix_group_members_user_id_status", "group_members", ["user_id", "status"], unique=False
    )
    # groups and conversations reference each other, so this one is added after
    # both tables exist (`use_alter` on the model).
    op.create_foreign_key(
        op.f("fk_groups_chat_conversation_id_conversations"),
        "groups",
        "conversations",
        ["chat_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_conversations_group_id_groups"),
        "conversations",
        "groups",
        ["group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column(
        "posts",
        sa.Column(
            "approval_status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                name="post_approval",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
    )
    op.add_column("posts", sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_posts_group_id_created_at_id", "posts", ["group_id", "created_at", "id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_posts_group_id_groups"),
        "posts",
        "groups",
        ["group_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "post_approval_only_in_groups", "posts", "approval_status IS NULL OR group_id IS NOT NULL"
    )
    op.create_check_constraint(
        "post_pin_only_in_groups", "posts", "pinned_at IS NULL OR group_id IS NOT NULL"
    )
    _replace_check("media_assets", "media_purpose", _in("purpose", MEDIA_PURPOSES_AFTER))
    _replace_check("audit_events", "audit_action", _in("action", AUDIT_ACTIONS_AFTER))


def downgrade() -> None:
    # Fails while group cover images or group audit events exist, which is right:
    # narrowing the constraint would orphan them.
    _replace_check("audit_events", "audit_action", _in("action", AUDIT_ACTIONS_BEFORE))
    _replace_check("media_assets", "media_purpose", _in("purpose", MEDIA_PURPOSES_BEFORE))
    op.drop_constraint(op.f("ck_posts_post_pin_only_in_groups"), "posts", type_="check")
    op.drop_constraint(op.f("ck_posts_post_approval_only_in_groups"), "posts", type_="check")
    op.drop_constraint(op.f("fk_posts_group_id_groups"), "posts", type_="foreignkey")
    op.drop_index("ix_posts_group_id_created_at_id", table_name="posts")
    op.drop_column("posts", "pinned_at")
    op.drop_column("posts", "approval_status")
    op.drop_constraint(
        op.f("fk_conversations_group_id_groups"), "conversations", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_groups_chat_conversation_id_conversations"), "groups", type_="foreignkey"
    )
    op.drop_index("ix_group_members_user_id_status", table_name="group_members")
    op.drop_index("ix_group_members_group_id_status_role", table_name="group_members")
    op.drop_table("group_members")
    op.drop_index(op.f("ix_group_invite_links_group_id"), table_name="group_invite_links")
    op.drop_table("group_invite_links")
    op.drop_index("ix_groups_visibility_member_count", table_name="groups")
    op.drop_index(op.f("ix_groups_owner_id"), table_name="groups")
    op.drop_index(
        "ix_groups_name_trgm",
        table_name="groups",
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.drop_index(op.f("ix_groups_deleted_at"), table_name="groups")
    op.drop_index(op.f("ix_groups_cell_res8"), table_name="groups")
    op.drop_index(op.f("ix_groups_category"), table_name="groups")
    op.drop_geospatial_index(
        "idx_groups_centroid", table_name="groups", postgresql_using="gist", column_name="centroid"
    )
    op.drop_geospatial_table("groups")
