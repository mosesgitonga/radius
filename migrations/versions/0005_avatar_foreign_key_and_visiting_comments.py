"""Avatar foreign key, and the visiting marker on comments

Two small changes, both anticipated.

* `users.avatar_media_id` gains its foreign key. The column was created in
  Phase 1 without one because `media_assets` did not exist yet; adding the
  constraint now is the cheap, constraint-only migration the brief asked the data
  model to allow for.
* `comments.is_visiting` records whether the author was exploring another area at
  the time, so a visitor's comment can be marked as such (decision 1.1, rule 3).
  Defaults to false, so existing rows need no backfill.

Revision ID: 0005
Revises: 0004
Created: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "comments",
        sa.Column("is_visiting", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_users_avatar_media_id_media_assets"),
        "users",
        "media_assets",
        ["avatar_media_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_users_avatar_media_id_media_assets"), "users", type_="foreignkey")
    op.drop_column("comments", "is_visiting")
