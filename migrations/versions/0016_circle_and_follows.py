"""Circle and follows

Circle requests and links, follows of service offerings, who may ask to join
someone's circle, a post's and a story's audience, and a post made as a service.
Decisions C.1-C.6.

Reviewed by hand:
- one pending request per pair, either way round (a partial unique index on the
  ordered pair);
- a link is stored once, low ID first, and is its own primary key;
- a follow is unique per follower and entity;
- `posts.service_post_id` is set to null if the service post is ever
  hard-deleted;
- autogenerate's duplicate enum constraints are removed;
- existing rows take the defaults: everyone may ask, and every post and story is
  for neighbours and circle.

Revision ID: 0016
Revises: 0015
Created: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "circle_links",
        sa.Column("user_low_id", sa.Uuid(), nullable=False),
        sa.Column("user_high_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "user_low_id < user_high_id", name=op.f("ck_circle_links_circle_link_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["user_high_id"],
            ["users.id"],
            name=op.f("fk_circle_links_user_high_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_low_id"],
            ["users.id"],
            name=op.f("fk_circle_links_user_low_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_low_id", "user_high_id", name=op.f("pk_circle_links")),
    )
    op.create_index(
        op.f("ix_circle_links_user_high_id"), "circle_links", ["user_high_id"], unique=False
    )
    op.create_table(
        "circle_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("requester_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "accepted",
                "declined",
                "cancelled",
                "expired",
                name="circle_request_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "requester_id <> recipient_id", name=op.f("ck_circle_requests_circle_request_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.id"],
            name=op.f("fk_circle_requests_recipient_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["users.id"],
            name=op.f("fk_circle_requests_requester_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_circle_requests")),
    )
    op.create_index(
        op.f("ix_circle_requests_created_at"), "circle_requests", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_circle_requests_recipient_id"), "circle_requests", ["recipient_id"], unique=False
    )
    op.create_index(
        op.f("ix_circle_requests_requester_id"), "circle_requests", ["requester_id"], unique=False
    )
    op.create_index(
        "uq_circle_requests_pending_pair",
        "circle_requests",
        [
            sa.literal_column("least(requester_id, recipient_id)"),
            sa.literal_column("greatest(requester_id, recipient_id)"),
        ],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "follows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("follower_id", sa.Uuid(), nullable=False),
        sa.Column(
            "entity_type",
            sa.Enum(
                "service_provider",
                name="follow_entity_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["follower_id"],
            ["users.id"],
            name=op.f("fk_follows_follower_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_follows")),
        sa.UniqueConstraint(
            "follower_id", "entity_type", "entity_id", name="uq_follows_follower_entity"
        ),
    )
    op.create_index("ix_follows_entity", "follows", ["entity_type", "entity_id"], unique=False)
    op.create_index(op.f("ix_follows_follower_id"), "follows", ["follower_id"], unique=False)
    op.add_column(
        "posts",
        sa.Column(
            "audience",
            sa.Enum(
                "neighbours_and_circle",
                "neighbours_only",
                name="audience",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="neighbours_and_circle",
            nullable=False,
        ),
    )
    op.add_column("posts", sa.Column("service_post_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_posts_service_post_id"), "posts", ["service_post_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_posts_service_post_id_service_posts"),
        "posts",
        "service_posts",
        ["service_post_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "stories",
        sa.Column(
            "audience",
            sa.Enum(
                "neighbours_and_circle",
                "neighbours_only",
                name="audience",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="neighbours_and_circle",
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "circle_request_policy",
            sa.Enum(
                "everyone",
                "nearby_only",
                "contacts_only",
                "nobody",
                name="circle_request_policy",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="everyone",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "circle_request_policy")
    op.drop_column("stories", "audience")
    op.drop_constraint(op.f("fk_posts_service_post_id_service_posts"), "posts", type_="foreignkey")
    op.drop_index(op.f("ix_posts_service_post_id"), table_name="posts")
    op.drop_column("posts", "service_post_id")
    op.drop_column("posts", "audience")
    op.drop_index(op.f("ix_follows_follower_id"), table_name="follows")
    op.drop_index("ix_follows_entity", table_name="follows")
    op.drop_table("follows")
    op.drop_index(
        "uq_circle_requests_pending_pair",
        table_name="circle_requests",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(op.f("ix_circle_requests_requester_id"), table_name="circle_requests")
    op.drop_index(op.f("ix_circle_requests_recipient_id"), table_name="circle_requests")
    op.drop_index(op.f("ix_circle_requests_created_at"), table_name="circle_requests")
    op.drop_table("circle_requests")
    op.drop_index(op.f("ix_circle_links_user_high_id"), table_name="circle_links")
    op.drop_table("circle_links")
