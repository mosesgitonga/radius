"""Background work for location: the density refresh.

`refresh_cell_activity` is what makes the radius adaptive. Counting active users
live, per request, would mean an aggregate over the whole users table on every
feed load; instead this runs every fifteen minutes and the engine reads a handful
of precomputed rows.

Written as SQL rather than through the ORM. It is three grouped aggregates and an
upsert over every user with a home location, and loading those rows into Python
objects to count them would be slower by orders of magnitude for no gain in
clarity — the SQL below is the clearer statement of what is wanted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.modules.users.models import HOME_PRESENCE_WINDOW_DAYS

logger = logging.getLogger(__name__)

# One statement per resolution, parameterised by the column holding that
# resolution's cell. The column name is interpolated from a fixed allowlist
# below, never from anything a caller controls.
_REFRESH_SQL = """
INSERT INTO cell_activity (
    resolution, cell, active_user_count, registered_user_count, computed_at
)
SELECT
    :resolution,
    {column},
    count(*) FILTER (WHERE last_active_at >= :active_since AND status = 'active'),
    count(*),
    :computed_at
FROM users
WHERE {column} IS NOT NULL
  AND deleted_at IS NULL
GROUP BY {column}
ON CONFLICT (resolution, cell) DO UPDATE SET
    active_user_count = EXCLUDED.active_user_count,
    registered_user_count = EXCLUDED.registered_user_count,
    computed_at = EXCLUDED.computed_at
"""

# Resolution to the user column holding it. Fixed in code: these are the only
# resolutions stored, and the mapping is what keeps the SQL above injection-free.
RESOLUTION_COLUMNS: dict[int, str] = {
    8: "home_cell_res8",
    7: "home_cell_res7",
    6: "home_cell_res6",
}


@dataclass(frozen=True, slots=True)
class DensityRefreshResult:
    cells_updated: int
    stale_cells_removed: int
    active_since: datetime


async def refresh_cell_activity(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> DensityRefreshResult:
    """Recompute per-cell population counts at every stored resolution.

    Resolution 8 feeds the radius engine; 7 and 6 feed k-anonymity, which needs to
    know how many registered users share a coarser cell before a label may name it.

    Rows for cells that no longer hold anyone are deleted rather than zeroed, by
    removing everything this run did not touch. Without that, a cell whose last
    resident moves away keeps its old count for ever, and the radius around it
    stays wrong indefinitely.
    """
    computed_at = now or datetime.now(UTC)
    active_since = computed_at - timedelta(days=settings.active_user_window_days)

    updated = 0
    for resolution, column in RESOLUTION_COLUMNS.items():
        updated += await execute_dml(
            session,
            text(_REFRESH_SQL.format(column=column)),
            {
                "resolution": resolution,
                "active_since": active_since,
                "computed_at": computed_at,
            },
        )

    removed = await execute_dml(
        session,
        text("DELETE FROM cell_activity WHERE computed_at < :computed_at"),
        {"computed_at": computed_at},
    )

    logger.info(
        "cell_activity_refreshed",
        extra={
            "cells_updated": updated,
            "stale_cells_removed": removed,
            "active_window_days": settings.active_user_window_days,
        },
    )
    return DensityRefreshResult(
        cells_updated=updated,
        stale_cells_removed=removed,
        active_since=active_since,
    )


# ---------------------------------------------------------------------------
# Home presence
# ---------------------------------------------------------------------------
# All three values are bound parameters, so this statement contains no string
# construction at all.
#
# Written with CAST(...) rather than PostgreSQL's `:param::type` shorthand:
# SQLAlchemy's text() parses `:name` as a bind parameter and the `::` that
# follows confuses it, which fails as "syntax error at or near :". CAST is also
# the form that reads unambiguously.
_ROLL_PRESENCE_SQL = """
UPDATE users
SET home_presence_mask = CASE
        WHEN (CAST(:today AS date) - home_presence_updated_on)
             >= CAST(:window AS integer)
            THEN 0
        ELSE (home_presence_mask << (CAST(:today AS date) - home_presence_updated_on))
             & CAST(:mask AS integer)
    END,
    home_presence_updated_on = CAST(:today AS date)
WHERE home_presence_updated_on IS NOT NULL
  AND home_presence_updated_on < CAST(:today AS date)
  AND deleted_at IS NULL
"""


async def roll_home_presence(session: AsyncSession, *, today: date | None = None) -> int:
    """Age every stale presence mask forward to today.

    The mask is normally rolled in the request that reports a location, so this
    only catches accounts that did not report one. It matters because the mask is
    positional: a user who was present for ten days and then stopped opening the
    app must not keep looking present, or the badge would reward absence.

    Done in one UPDATE rather than row by row: this touches every account that
    missed a day, which at launch is most of them.
    """
    target_day = today or datetime.now(UTC).date()
    rolled = await execute_dml(
        session,
        text(_ROLL_PRESENCE_SQL),
        {
            "today": target_day,
            "window": HOME_PRESENCE_WINDOW_DAYS,
            "mask": (1 << HOME_PRESENCE_WINDOW_DAYS) - 1,
        },
    )
    logger.info("home_presence_rolled", extra={"users_updated": rolled})
    return rolled
