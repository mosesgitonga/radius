"""Provisional home

Whether a home was set away from home at sign-up. Decision M.10.

Reviewed by hand: false for every existing account, whose homes were all set as
real ones. No index: read per account, never queried across accounts.

Revision ID: 0018
Revises: 0017
Created: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "home_provisional", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "home_provisional")
