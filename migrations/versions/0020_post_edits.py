"""Post edits

Editing a post's text or category (decision 2.22): when it was last edited, and
what it said before each edit, for moderators.

Reviewed by hand: earlier versions go with their post; existing posts have never
been edited, so `edited_at` starts empty.

Revision ID: 0020
Revises: 0019
Created: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "post_edits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.String(length=4000), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column(
            "edited_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], name=op.f("fk_post_edits_post_id_posts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_post_edits")),
    )
    op.create_index(op.f("ix_post_edits_post_id"), "post_edits", ["post_id"], unique=False)
    op.add_column("posts", sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "edited_at")
    op.drop_index(op.f("ix_post_edits_post_id"), table_name="post_edits")
    op.drop_table("post_edits")
