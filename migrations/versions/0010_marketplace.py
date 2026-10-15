"""Marketplace

`listings`, `listing_images` and `saved_listings`, and the listing conversation:

* `listings.search_vector` is a stored generated `tsvector` (`simple`
  configuration, title weighted over description) with a GIN index — full-text
  search with no trigger to maintain.
* `listings.coarse_centroid` is the res-8 cell centre, used only to sort by
  distance band; see `app/modules/marketplace/models.py`.
* `conversations.listing_id` becomes a foreign key, and a partial unique index
  allows one listing conversation per listing per buyer.

Reviewed by hand: the free-means-zero and sold-has-time checks, RESTRICT on
`listing_images.media_id`, and that autogenerate's duplicate copies of each enum
constraint are removed (as in 0007 and 0009).

Revision ID: 0010
Revises: 0009
Created: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_geospatial_table(
        "listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seller_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.String(length=20000), server_default="", nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column(
            "condition",
            sa.Enum(
                "new",
                "like_new",
                "good",
                "fair",
                "for_parts",
                name="listing_condition",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "pricing",
            sa.Enum(
                "fixed",
                "negotiable",
                "free",
                name="listing_pricing",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("price_kes", sa.BigInteger(), nullable=False),
        sa.Column(
            "availability",
            sa.Enum(
                "available",
                "reserved",
                "sold",
                name="listing_availability",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="available",
            nullable=False,
        ),
        sa.Column("sold_to_id", sa.Uuid(), nullable=True),
        sa.Column("sold_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("reach_estimate", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
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
            "availability <> 'sold' OR sold_at IS NOT NULL",
            name=op.f("ck_listings_listing_sold_has_time"),
        ),
        sa.CheckConstraint(
            "pricing <> 'free' OR price_kes = 0", name=op.f("ck_listings_listing_free_means_zero")
        ),
        sa.CheckConstraint("length(title) > 0", name=op.f("ck_listings_listing_title_not_empty")),
        sa.CheckConstraint("price_kes >= 0", name=op.f("ck_listings_listing_price_non_negative")),
        sa.ForeignKeyConstraint(
            ["area_id"], ["areas.id"], name=op.f("fk_listings_area_id_areas"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["seller_id"],
            ["users.id"],
            name=op.f("fk_listings_seller_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sold_to_id"],
            ["users.id"],
            name=op.f("fk_listings_sold_to_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listings")),
    )
    op.create_geospatial_index(
        "idx_listings_centroid",
        "listings",
        ["centroid"],
        unique=False,
        postgresql_using="gist",
        postgresql_ops={},
    )
    op.create_index(
        "ix_listings_availability_created_at_id",
        "listings",
        ["availability", "created_at", "id"],
        unique=False,
    )
    op.create_index(op.f("ix_listings_category"), "listings", ["category"], unique=False)
    op.create_index(op.f("ix_listings_cell_res6"), "listings", ["cell_res6"], unique=False)
    op.create_index(op.f("ix_listings_cell_res7"), "listings", ["cell_res7"], unique=False)
    op.create_index(op.f("ix_listings_cell_res8"), "listings", ["cell_res8"], unique=False)
    op.create_index(op.f("ix_listings_cell_res9"), "listings", ["cell_res9"], unique=False)
    op.create_index(op.f("ix_listings_content_hash"), "listings", ["content_hash"], unique=False)
    op.create_index(op.f("ix_listings_deleted_at"), "listings", ["deleted_at"], unique=False)
    op.create_index(op.f("ix_listings_expires_at"), "listings", ["expires_at"], unique=False)
    op.create_index("ix_listings_price_kes_id", "listings", ["price_kes", "id"], unique=False)
    op.create_index(
        "ix_listings_search_vector",
        "listings",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(op.f("ix_listings_seller_id"), "listings", ["seller_id"], unique=False)
    op.create_index(
        "ix_listings_seller_id_created_at", "listings", ["seller_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_listings_sold_to_id"), "listings", ["sold_to_id"], unique=False)
    op.create_table(
        "listing_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("media_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_listing_images_listing_image_position_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_listing_images_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_assets.id"],
            name=op.f("fk_listing_images_media_id_media_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listing_images")),
        sa.UniqueConstraint("listing_id", "media_id", name="uq_listing_images_listing_id_media_id"),
        sa.UniqueConstraint("listing_id", "position", name="uq_listing_images_listing_id_position"),
    )
    op.create_index(
        op.f("ix_listing_images_listing_id"), "listing_images", ["listing_id"], unique=False
    )
    op.create_table(
        "saved_listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
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
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_saved_listings_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_saved_listings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_listings")),
        sa.UniqueConstraint("user_id", "listing_id", name="uq_saved_listings_user_id_listing_id"),
    )
    op.create_index(
        op.f("ix_saved_listings_listing_id"), "saved_listings", ["listing_id"], unique=False
    )
    op.create_index(
        "ix_saved_listings_user_id_created_at",
        "saved_listings",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_conversations_listing_id_created_by_id",
        "conversations",
        ["listing_id", "created_by_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'listing'"),
    )
    op.create_foreign_key(
        op.f("fk_conversations_listing_id_listings"),
        "conversations",
        "listings",
        ["listing_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_conversations_listing_id_listings"), "conversations", type_="foreignkey"
    )
    op.drop_index(
        "uq_conversations_listing_id_created_by_id",
        table_name="conversations",
        postgresql_where=sa.text("kind = 'listing'"),
    )
    op.drop_index("ix_saved_listings_user_id_created_at", table_name="saved_listings")
    op.drop_index(op.f("ix_saved_listings_listing_id"), table_name="saved_listings")
    op.drop_table("saved_listings")
    op.drop_index(op.f("ix_listing_images_listing_id"), table_name="listing_images")
    op.drop_table("listing_images")
    op.drop_index(op.f("ix_listings_sold_to_id"), table_name="listings")
    op.drop_index("ix_listings_seller_id_created_at", table_name="listings")
    op.drop_index(op.f("ix_listings_seller_id"), table_name="listings")
    op.drop_index("ix_listings_search_vector", table_name="listings", postgresql_using="gin")
    op.drop_index("ix_listings_price_kes_id", table_name="listings")
    op.drop_index(op.f("ix_listings_expires_at"), table_name="listings")
    op.drop_index(op.f("ix_listings_deleted_at"), table_name="listings")
    op.drop_index(op.f("ix_listings_content_hash"), table_name="listings")
    op.drop_index(op.f("ix_listings_cell_res9"), table_name="listings")
    op.drop_index(op.f("ix_listings_cell_res8"), table_name="listings")
    op.drop_index(op.f("ix_listings_cell_res7"), table_name="listings")
    op.drop_index(op.f("ix_listings_cell_res6"), table_name="listings")
    op.drop_index(op.f("ix_listings_category"), table_name="listings")
    op.drop_index("ix_listings_availability_created_at_id", table_name="listings")
    op.drop_geospatial_index(
        "idx_listings_centroid",
        table_name="listings",
        postgresql_using="gist",
        column_name="centroid",
    )
    op.drop_geospatial_table("listings")
