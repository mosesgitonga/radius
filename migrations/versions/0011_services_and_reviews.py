"""Services and reviews

`service_posts` (offering, seeking and hiring in one table, each kind held to its
own fields by check constraints) and `reviews`, plus:

* `conversations.service_post_id`, with a partial unique index for one
  conversation per post per person, and the `service` conversation kind;
* `reports.target_type` gains `review`; `audit_events.action` gains
  `content_flagged`.

Literal value lists, as in 0006, so this means the same thing after the enums
grow. Reviewed by hand: the per-kind field checks, the one-live-offering-per-
category and one-review-per-pair-per-context partial unique indexes, and that
autogenerate's duplicate enum constraints are removed.

Revision ID: 0011
Revises: 0010
Created: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONVERSATION_KINDS_BEFORE = ("direct", "group", "listing")
CONVERSATION_KINDS_AFTER = ("direct", "group", "listing", "service")
REPORT_TARGETS_BEFORE = (
    "post",
    "comment",
    "story",
    "user",
    "message",
    "listing",
    "service_post",
    "event",
    "group",
)
REPORT_TARGETS_AFTER = (*REPORT_TARGETS_BEFORE[:5], "review", *REPORT_TARGETS_BEFORE[5:])
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
    "group_deleted",
    "group_member_removed",
    "group_member_banned",
    "group_member_unbanned",
    "group_role_changed",
    "group_ownership_transferred",
    "group_post_removed",
)
AUDIT_ACTIONS_AFTER = (*AUDIT_ACTIONS_BEFORE[:17], "content_flagged", *AUDIT_ACTIONS_BEFORE[17:])


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_check(table: str, suffix: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{suffix}"), table, type_="check")
    op.create_check_constraint(suffix, table, condition)


def upgrade() -> None:
    op.create_geospatial_table(
        "service_posts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "offering",
                "seeking",
                "hiring",
                name="service_kind",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.String(length=20000), server_default="", nullable=False),
        sa.Column("rate_info", sa.String(length=1000), nullable=True),
        sa.Column("availability_info", sa.String(length=1000), nullable=True),
        sa.Column("travel_km", sa.Integer(), nullable=True),
        sa.Column("budget_info", sa.String(length=1000), nullable=True),
        sa.Column(
            "job_type",
            sa.Enum(
                "full_time",
                "part_time",
                "casual",
                "contract",
                name="job_type",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("pay_min_kes", sa.BigInteger(), nullable=True),
        sa.Column("pay_max_kes", sa.BigInteger(), nullable=True),
        sa.Column(
            "pay_period",
            sa.Enum(
                "hour",
                "day",
                "week",
                "month",
                "one_off",
                name="pay_period",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "visible",
                "hidden_pending_review",
                "removed",
                name="content_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="visible",
            nullable=False,
        ),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "flagged_phrases",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "coarse_centroid",
            Geography(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeogFromText",
                name="geography",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || setweight(to_tsvector('simple', coalesce(description, '')), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("cell_res9", sa.BigInteger(), nullable=False),
        sa.Column("cell_res8", sa.BigInteger(), nullable=False),
        sa.Column("cell_res7", sa.BigInteger(), nullable=False),
        sa.Column("cell_res6", sa.BigInteger(), nullable=False),
        sa.Column(
            "centroid",
            Geography(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeogFromText",
                name="geography",
                nullable=False,
            ),
            nullable=False,
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
            "kind <> 'offering' OR expires_at IS NULL",
            name=op.f("ck_service_posts_service_post_offering_no_expiry"),
        ),
        sa.CheckConstraint(
            "kind = 'hiring' OR (job_type IS NULL AND pay_min_kes IS NULL AND pay_max_kes IS NULL AND pay_period IS NULL)",
            name=op.f("ck_service_posts_service_post_pay_only_for_hiring"),
        ),
        sa.CheckConstraint(
            "kind = 'offering' OR (travel_km IS NULL AND rate_info IS NULL)",
            name=op.f("ck_service_posts_service_post_rates_only_for_offering"),
        ),
        sa.CheckConstraint(
            "length(title) > 0", name=op.f("ck_service_posts_service_post_title_not_empty")
        ),
        sa.CheckConstraint(
            "pay_min_kes IS NULL OR pay_max_kes IS NULL OR pay_min_kes <= pay_max_kes",
            name=op.f("ck_service_posts_service_post_pay_range_ordered"),
        ),
        sa.CheckConstraint(
            "travel_km IS NULL OR (travel_km >= 1 AND travel_km <= 200)",
            name=op.f("ck_service_posts_service_post_travel_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_service_posts_area_id_areas"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_service_posts_author_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_posts")),
    )
    op.create_geospatial_index(
        "idx_service_posts_centroid",
        "service_posts",
        ["centroid"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(
        op.f("ix_service_posts_author_id"), "service_posts", ["author_id"], unique=False
    )
    op.create_index(op.f("ix_service_posts_category"), "service_posts", ["category"], unique=False)
    op.create_index(
        op.f("ix_service_posts_cell_res6"), "service_posts", ["cell_res6"], unique=False
    )
    op.create_index(
        op.f("ix_service_posts_cell_res7"), "service_posts", ["cell_res7"], unique=False
    )
    op.create_index(
        op.f("ix_service_posts_cell_res8"), "service_posts", ["cell_res8"], unique=False
    )
    op.create_index(
        op.f("ix_service_posts_cell_res9"), "service_posts", ["cell_res9"], unique=False
    )
    op.create_index(
        op.f("ix_service_posts_deleted_at"), "service_posts", ["deleted_at"], unique=False
    )
    op.create_index(
        "ix_service_posts_kind_created_at_id",
        "service_posts",
        ["kind", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_service_posts_search_vector",
        "service_posts",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "uq_service_posts_live_offering",
        "service_posts",
        ["author_id", "category"],
        unique=True,
        postgresql_where=sa.text("kind = 'offering' AND deleted_at IS NULL"),
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column(
            "context_type",
            sa.Enum(
                "listing",
                "service_post",
                name="review_context",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("context_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("body", sa.String(length=10000), server_default="", nullable=False),
        sa.Column("reply_body", sa.String(length=10000), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "visible",
                "hidden_pending_review",
                "removed",
                name="content_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="visible",
            nullable=False,
        ),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
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
            "rating >= 1 AND rating <= 5", name=op.f("ck_reviews_review_rating_in_range")
        ),
        sa.CheckConstraint("reviewer_id <> subject_id", name=op.f("ck_reviews_review_not_of_self")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_reviews_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["users.id"],
            name=op.f("fk_reviews_reviewer_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["users.id"],
            name=op.f("fk_reviews_subject_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
    )
    op.create_index(op.f("ix_reviews_deleted_at"), "reviews", ["deleted_at"], unique=False)
    op.create_index(op.f("ix_reviews_reviewer_id"), "reviews", ["reviewer_id"], unique=False)
    op.create_index(
        "ix_reviews_subject_id_created_at", "reviews", ["subject_id", "created_at"], unique=False
    )
    op.create_index(
        "uq_reviews_one_per_pair_per_context",
        "reviews",
        ["reviewer_id", "subject_id", "context_type", "context_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.add_column("conversations", sa.Column("service_post_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_conversations_service_post_id"), "conversations", ["service_post_id"], unique=False
    )
    op.create_index(
        "uq_conversations_service_post_id_created_by_id",
        "conversations",
        ["service_post_id", "created_by_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'service'"),
    )
    op.create_foreign_key(
        op.f("fk_conversations_service_post_id_service_posts"),
        "conversations",
        "service_posts",
        ["service_post_id"],
        ["id"],
        ondelete="SET NULL",
    )

    _replace_check("conversations", "conversation_kind", _in("kind", CONVERSATION_KINDS_AFTER))
    _replace_check("reports", "report_target_type", _in("target_type", REPORT_TARGETS_AFTER))
    _replace_check("audit_events", "audit_action", _in("action", AUDIT_ACTIONS_AFTER))


def downgrade() -> None:
    # Fails while service conversations, review reports or flag audit events exist,
    # which is right: narrowing would orphan them.
    _replace_check("audit_events", "audit_action", _in("action", AUDIT_ACTIONS_BEFORE))
    _replace_check("reports", "report_target_type", _in("target_type", REPORT_TARGETS_BEFORE))
    _replace_check("conversations", "conversation_kind", _in("kind", CONVERSATION_KINDS_BEFORE))
    op.drop_constraint(
        op.f("fk_conversations_service_post_id_service_posts"), "conversations", type_="foreignkey"
    )
    op.drop_index(
        "uq_conversations_service_post_id_created_by_id",
        table_name="conversations",
        postgresql_where=sa.text("kind = 'service'"),
    )
    op.drop_index(op.f("ix_conversations_service_post_id"), table_name="conversations")
    op.drop_column("conversations", "service_post_id")
    op.drop_index(
        "uq_reviews_one_per_pair_per_context",
        table_name="reviews",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index("ix_reviews_subject_id_created_at", table_name="reviews")
    op.drop_index(op.f("ix_reviews_reviewer_id"), table_name="reviews")
    op.drop_index(op.f("ix_reviews_deleted_at"), table_name="reviews")
    op.drop_table("reviews")
    op.drop_index(
        "uq_service_posts_live_offering",
        table_name="service_posts",
        postgresql_where=sa.text("kind = 'offering' AND deleted_at IS NULL"),
    )
    op.drop_index(
        "ix_service_posts_search_vector", table_name="service_posts", postgresql_using="gin"
    )
    op.drop_index("ix_service_posts_kind_created_at_id", table_name="service_posts")
    op.drop_index(op.f("ix_service_posts_deleted_at"), table_name="service_posts")
    op.drop_index(op.f("ix_service_posts_cell_res9"), table_name="service_posts")
    op.drop_index(op.f("ix_service_posts_cell_res8"), table_name="service_posts")
    op.drop_index(op.f("ix_service_posts_cell_res7"), table_name="service_posts")
    op.drop_index(op.f("ix_service_posts_cell_res6"), table_name="service_posts")
    op.drop_index(op.f("ix_service_posts_category"), table_name="service_posts")
    op.drop_index(op.f("ix_service_posts_author_id"), table_name="service_posts")
    op.drop_geospatial_index(
        "idx_service_posts_centroid",
        table_name="service_posts",
        postgresql_using="gist",
        column_name="centroid",
    )
    op.drop_geospatial_table("service_posts")
