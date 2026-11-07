"""Comment votes

Up and down votes on comments, and each comment's net score, which ranks
top-level comments (decision 2.21).

Reviewed by hand: one vote per person per comment, valued -1 or +1; votes go with
their comment or their voter; existing comments start at a score of 0. The
ranking index matches the listing's order: score descending, then oldest.

Revision ID: 0019
Revises: 0018
Created: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comment_votes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("comment_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("value IN (-1, 1)", name=op.f("ck_comment_votes_comment_vote_value")),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["comments.id"],
            name=op.f("fk_comment_votes_comment_id_comments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_comment_votes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comment_votes")),
        sa.UniqueConstraint("user_id", "comment_id", name="uq_comment_votes_user_id_comment_id"),
    )
    op.create_index(
        op.f("ix_comment_votes_comment_id"), "comment_votes", ["comment_id"], unique=False
    )
    op.create_index(op.f("ix_comment_votes_user_id"), "comment_votes", ["user_id"], unique=False)
    op.add_column("comments", sa.Column("score", sa.Integer(), server_default="0", nullable=False))
    op.create_index(
        "ix_comments_post_id_parent_id_score_created_at",
        "comments",
        ["post_id", "parent_id", sa.literal_column("score DESC"), "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_comments_post_id_parent_id_score_created_at", table_name="comments")
    op.drop_column("comments", "score")
    op.drop_index(op.f("ix_comment_votes_user_id"), table_name="comment_votes")
    op.drop_index(op.f("ix_comment_votes_comment_id"), table_name="comment_votes")
    op.drop_table("comment_votes")
