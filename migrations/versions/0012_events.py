"""Events

`events`, `event_rsvps` and `event_reminders_sent`, and
`listings.expiry_reminded_at` for the listing-expiry reminder.

An event's location is the venue's cell and centre, derived from coordinates the
organiser picked and then discarded — no coordinate column exists. Reviewed by
hand: the ends-after-start, group-visibility and link-only-for-private checks, the
unique (event, user, offset) constraint that makes reminders idempotent, and that
autogenerate's duplicate enum constraints are removed.

Revision ID: 0012
Revises: 0011
Created: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_geospatial_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organiser_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.String(length=20000), server_default="", nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "party",
                "church",
                "sports",
                "harambee",
                "clean_up",
                "meetup",
                "other",
                name="event_type",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("venue_name", sa.String(length=500), nullable=False),
        sa.Column("venue_details", sa.String(length=5000), nullable=True),
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
        sa.Column("cover_media_id", sa.Uuid(), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column(
            "visibility",
            sa.Enum(
                "public",
                "group",
                "private",
                name="event_visibility",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "attendee_visibility",
            sa.Enum(
                "everyone",
                "attendees",
                "organiser",
                name="attendee_visibility",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="everyone",
            nullable=False,
        ),
        sa.Column("invite_token_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "event_status",
            sa.Enum(
                "scheduled",
                "cancelled",
                name="event_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="scheduled",
            nullable=False,
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(length=1000), nullable=True),
        sa.Column("going_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("interested_count", sa.Integer(), server_default="0", nullable=False),
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
            "event_status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name=op.f("ck_events_event_cancelled_has_time"),
        ),
        sa.CheckConstraint(
            "invite_token_hash IS NULL OR visibility = 'private'",
            name=op.f("ck_events_event_link_only_for_private"),
        ),
        sa.CheckConstraint(
            "visibility <> 'group' OR group_id IS NOT NULL",
            name=op.f("ck_events_event_group_visibility_has_group"),
        ),
        sa.CheckConstraint(
            "capacity IS NULL OR capacity > 0", name=op.f("ck_events_event_capacity_positive")
        ),
        sa.CheckConstraint("end_at > start_at", name=op.f("ck_events_event_ends_after_it_starts")),
        sa.CheckConstraint(
            "going_count >= 0 AND interested_count >= 0",
            name=op.f("ck_events_event_counts_non_negative"),
        ),
        sa.CheckConstraint("length(title) > 0", name=op.f("ck_events_event_title_not_empty")),
        sa.ForeignKeyConstraint(
            ["area_id"], ["areas.id"], name=op.f("fk_events_area_id_areas"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["cover_media_id"],
            ["media_assets.id"],
            name=op.f("fk_events_cover_media_id_media_assets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_events_group_id_groups"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organiser_id"],
            ["users.id"],
            name=op.f("fk_events_organiser_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("invite_token_hash", name=op.f("uq_events_invite_token_hash")),
    )
    op.create_geospatial_index(
        "idx_events_centroid",
        "events",
        ["centroid"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(op.f("ix_events_cell_res6"), "events", ["cell_res6"], unique=False)
    op.create_index(op.f("ix_events_cell_res7"), "events", ["cell_res7"], unique=False)
    op.create_index(op.f("ix_events_cell_res8"), "events", ["cell_res8"], unique=False)
    op.create_index(op.f("ix_events_cell_res9"), "events", ["cell_res9"], unique=False)
    op.create_index(op.f("ix_events_deleted_at"), "events", ["deleted_at"], unique=False)
    op.create_index(op.f("ix_events_group_id"), "events", ["group_id"], unique=False)
    op.create_index(op.f("ix_events_organiser_id"), "events", ["organiser_id"], unique=False)
    op.create_index(op.f("ix_events_start_at"), "events", ["start_at"], unique=False)
    op.create_index(
        "ix_events_visibility_start_at_id", "events", ["visibility", "start_at", "id"], unique=False
    )
    op.create_table(
        "event_reminders_sent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_reminders_sent_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_event_reminders_sent_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_reminders_sent")),
        sa.UniqueConstraint(
            "event_id",
            "user_id",
            "offset_minutes",
            name="uq_event_reminders_sent_event_id_user_id_offset_minutes",
        ),
    )
    op.create_table(
        "event_rsvps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "response",
            sa.Enum(
                "going",
                "interested",
                "not_going",
                name="rsvp_response",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column(
            "approval",
            sa.Enum(
                "approved",
                "pending",
                "declined",
                name="rsvp_approval",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="approved",
            nullable=False,
        ),
        sa.Column("invited_by_id", sa.Uuid(), nullable=True),
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
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_rsvps_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_id"],
            ["users.id"],
            name=op.f("fk_event_rsvps_invited_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_event_rsvps_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_rsvps")),
        sa.UniqueConstraint("event_id", "user_id", name="uq_event_rsvps_event_id_user_id"),
    )
    op.create_index(
        "ix_event_rsvps_event_id_response", "event_rsvps", ["event_id", "response"], unique=False
    )
    op.create_index(op.f("ix_event_rsvps_user_id"), "event_rsvps", ["user_id"], unique=False)
    op.add_column(
        "listings", sa.Column("expiry_reminded_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("listings", "expiry_reminded_at")
    op.drop_index(op.f("ix_event_rsvps_user_id"), table_name="event_rsvps")
    op.drop_index("ix_event_rsvps_event_id_response", table_name="event_rsvps")
    op.drop_table("event_rsvps")
    op.drop_table("event_reminders_sent")
    op.drop_index("ix_events_visibility_start_at_id", table_name="events")
    op.drop_index(op.f("ix_events_start_at"), table_name="events")
    op.drop_index(op.f("ix_events_organiser_id"), table_name="events")
    op.drop_index(op.f("ix_events_group_id"), table_name="events")
    op.drop_index(op.f("ix_events_deleted_at"), table_name="events")
    op.drop_index(op.f("ix_events_cell_res9"), table_name="events")
    op.drop_index(op.f("ix_events_cell_res8"), table_name="events")
    op.drop_index(op.f("ix_events_cell_res7"), table_name="events")
    op.drop_index(op.f("ix_events_cell_res6"), table_name="events")
    op.drop_geospatial_index(
        "idx_events_centroid", table_name="events", postgresql_using="gist", column_name="centroid"
    )
    op.drop_geospatial_table("events")
