"""Allow a purged account to hold no phone number

An erasure should leave no number behind, so `phone_number` becomes nullable and
`purge_user` sets it to null.

The first attempt at this wrote a `purged:<uuid>` placeholder instead, to keep the
unique index satisfied. It did not fit the column — 43 characters against
VARCHAR(20) — so the nightly retention job failed on its first real account and
nothing was ever purged, a Data Protection Act failure visible only in worker
logs. Widening the column would have worked, but null is the more honest record
of an erasure, and PostgreSQL permits many nulls in a unique index, so nothing
has to be invented to keep rows distinct.

The downgrade restores NOT NULL and will fail if any account has already been
purged. That is correct: there would be no number to put back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("users", "phone_number", existing_type=sa.VARCHAR(length=20), nullable=True)


def downgrade() -> None:
    op.alter_column("users", "phone_number", existing_type=sa.VARCHAR(length=20), nullable=False)
