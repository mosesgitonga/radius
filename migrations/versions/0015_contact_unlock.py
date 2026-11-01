"""Contact unlock and the daily presence check

When an account was first at home on enough days in a row to start conversations
with strangers, and the last day the app checked whether the phone was at home.
Decision M.7.

Reviewed by hand: both nullable, no index (read per account, never queried across
accounts).

Revision ID: 0015
Revises: 0014
Created: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("contact_unlocked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("presence_checked_on", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "presence_checked_on")
    op.drop_column("users", "contact_unlocked_at")
