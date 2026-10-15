"""`EXPLAIN (ANALYZE, BUFFERS)` the feed and marketplace queries, as they run.

    DATABASE_URL=.../myradius_perf uv run python -m scripts.explain

Runs the real service functions for one sample user, captures the main statement
of each with its parameters, and prints PostgreSQL's plan for it. The plan is for
the query the application actually sends, not a hand-written approximation.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.db import dispose_engine, get_engine, session_scope
from app.core.pagination import PageParams
from app.core.redis import get_redis
from app.modules.feed import service as feed_service
from app.modules.marketplace import service as marketplace_service
from app.modules.users.models import User

CAPTURE = {"feed": "SELECT posts.id", "marketplace": "SELECT listings.id"}


async def main() -> int:
    settings = get_settings()
    if not (make_url(settings.database_url).database or "").endswith("_perf"):
        sys.stdout.write("Refusing: not a _perf database.\n")
        return 1
    captured: dict[str, tuple[str, Any]] = {}

    @event.listens_for(get_engine().sync_engine, "before_cursor_execute")
    def _capture(conn: Any, cursor: Any, statement: str, parameters: Any, *rest: Any) -> None:
        for name, prefix in CAPTURE.items():
            if statement.lstrip().startswith(prefix) and name not in captured:
                captured[name] = (statement, parameters)

    redis = get_redis()
    page = PageParams(limit=settings.pagination_default_limit, cursor=None)
    async with session_scope() as session:
        user_id = await session.scalar(
            text(
                "SELECT id FROM users WHERE home_cell_res9 IS NOT NULL "
                "ORDER BY md5(id::text) LIMIT 1 OFFSET 7"
            )
        )
        viewer = await session.get(User, user_id)
        assert viewer is not None  # noqa: S101
        await feed_service.feed_page(session, redis, viewer=viewer, params=page, settings=settings)
        await marketplace_service.browse(
            session,
            redis,
            viewer=viewer,
            query=None,
            category=None,
            condition=None,
            min_price=None,
            max_price=None,
            sort=None,
            distance_km=None,
            mode="home",
            params=page,
            settings=settings,
        )
        connection = await session.connection()
        for name, (statement, parameters) in captured.items():
            sys.stdout.write(f"\n=== {name} ===\n")
            rows = await connection.exec_driver_sql(
                f"EXPLAIN (ANALYZE, BUFFERS) {statement}", parameters
            )
            for (line,) in rows:
                sys.stdout.write(f"{line}\n")
    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
