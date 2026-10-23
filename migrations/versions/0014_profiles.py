"""Profiles

A bio and a cover photo on each account, and the `cover` upload purpose.

Reviewed by hand: the cover's foreign key is `SET NULL` like the avatar's, the bio
is bounded by its column, and the purpose constraint is widened rather than
dropped.

Revision ID: 0014
Revises: 0013
Created: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEDIA_PURPOSES_BEFORE = ("avatar", "post", "story", "message", "group", "listing", "event_cover")
MEDIA_PURPOSES_AFTER = (*MEDIA_PURPOSES_BEFORE, "cover")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _replace_check(table: str, suffix: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{suffix}"), table, type_="check")
    op.create_check_constraint(suffix, table, condition)


def upgrade() -> None:
    op.add_column("users", sa.Column("bio", sa.String(length=160), nullable=True))
    op.add_column("users", sa.Column("cover_media_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_users_cover_media_id_media_assets"),
        "users",
        "media_assets",
        ["cover_media_id"],
        ["id"],
        ondelete="SET NULL",
    )
    _replace_check("media_assets", "media_purpose", _in("purpose", MEDIA_PURPOSES_AFTER))


def downgrade() -> None:
    # Fails while cover uploads exist, which is right: narrowing the constraint
    # would orphan them.
    _replace_check("media_assets", "media_purpose", _in("purpose", MEDIA_PURPOSES_BEFORE))
    op.drop_constraint(op.f("fk_users_cover_media_id_media_assets"), "users", type_="foreignkey")
    op.drop_column("users", "cover_media_id")
    op.drop_column("users", "bio")
