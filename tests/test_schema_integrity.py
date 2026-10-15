"""The database enforces what the models declare.

Two failures this catches, both silent until a bad row breaks a read:

1. An enum column with **no** CHECK constraint. That was every enum column in
   Phases 1 and 2: SQLAlchemy stopped emitting the constraint by default in 1.4,
   and `create_constraint=True` was never set. Found when a direct insert of
   `status = 'definitely_not_a_status'` into `users` succeeded.
2. A value added to a Python enum **without a migration** to widen the constraint.
   The application would then fail to write the new value at all, which this test
   reports as a precise diff rather than as an IntegrityError in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Enum, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.idempotency
import app.modules.auth.models
import app.modules.feed.models
import app.modules.groups.models
import app.modules.location.models
import app.modules.marketplace.models
import app.modules.media.models
import app.modules.messaging.models
import app.modules.moderation.models
import app.modules.notifications.models
import app.modules.users.models  # noqa: F401
from app.core.models import Base

ENUM_COLUMNS = sorted(
    (table.name, column.name, tuple(column.type.enums))
    for table in Base.metadata.tables.values()
    for column in table.columns
    if isinstance(column.type, Enum)
    # Throwaway test-only tables never exist on disk.
    and not table.name.startswith("test_")
)


def test_there_are_enum_columns_to_check() -> None:
    # A floor, so a broken discovery (a model module not imported) fails loudly.
    assert len(ENUM_COLUMNS) >= 19


@pytest.mark.parametrize(
    ("table", "column", "values"),
    ENUM_COLUMNS,
    ids=[f"{table}.{column}" for table, column, _ in ENUM_COLUMNS],
)
async def test_enum_column_is_constrained_to_exactly_its_values(
    db_session: AsyncSession, table: str, column: str, values: tuple[str, ...]
) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE contype = 'c' AND conrelid = CAST(:table AS regclass)"
            ),
            {"table": table},
        )
    ).scalars()
    # The column name in parentheses, so `status` does not also match the
    # constraint on `approval_status`.
    definitions = [
        definition
        for definition in rows
        if f"({column})::text = ANY" in definition or f"({column} = ANY" in definition
    ]

    assert definitions, f"{table}.{column} has no CHECK constraint on its values"

    # pg_get_constraintdef renders "... = ANY ((ARRAY['a'::character varying, ...])::text[])".
    allowed = {
        part.split("'")[1]
        for definition in definitions
        for part in definition.split("ARRAY[", 1)[1].split(",")
        if "'" in part
    }
    missing = set(values) - allowed
    extra = allowed - set(values)
    assert not missing, (
        f"{table}.{column}: the Python enum has {sorted(missing)} but the database "
        "would reject them — add a migration that widens the constraint"
    )
    assert not extra, (
        f"{table}.{column}: the database allows {sorted(extra)}, which the Python "
        "enum no longer declares"
    )


async def test_a_bogus_value_is_rejected(db_session: AsyncSession) -> None:
    """The original finding, kept as a regression test."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO users (id, phone_number, display_name, status, "
                    "adult_confirmed_at, last_active_at, home_presence_mask, "
                    "is_verified_neighbour, rating_count, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), '+254700000999', 'x', "
                    "'definitely_not_a_status', now(), now(), 0, false, 0, now(), now())"
                )
            )
