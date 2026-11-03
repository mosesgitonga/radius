"""People nearby

Whether a person chose to be listed in People nearby. Decision M.8.

Reviewed by hand: off for every existing account, so nobody becomes findable
without deciding to. No index: the list query is driven by the spatial index on
`home_centroid`, and this only filters what it finds.

Revision ID: 0017
Revises: 0016
Created: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("show_in_nearby", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "show_in_nearby")
