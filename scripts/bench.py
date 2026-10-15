"""Time the feed and the marketplace against a seeded database.

    DATABASE_URL=.../myradius_perf RATE_LIMIT_ENABLED=false uv run python -m scripts.bench

Requests go through the whole application in-process — authentication, the radius
engine, the query, the views, serialisation — one at a time, as a sample of real
users, and the server-side time of each is recorded. Network time is not included:
this measures what the server can change.

`--statements` also reports which SQL statements the time went on, slowest total
first, which is where `EXPLAIN (ANALYZE, BUFFERS)` should be pointed next.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import defaultdict
from typing import Any

import httpx
from sqlalchemy import event, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.db import dispose_engine, get_engine, session_scope
from app.main import create_app
from app.modules.auth import service as auth_service
from app.modules.users.models import User

TARGET_P95_MS = 200.0

ENDPOINTS: dict[str, tuple[str, dict[str, str]]] = {
    "feed": ("/v1/feed", {}),
    "feed page 2": ("/v1/feed", {"page": "2"}),
    "marketplace newest": ("/v1/listings", {}),
    "marketplace search": ("/v1/listings", {"q": "sofa"}),
    "marketplace nearest": ("/v1/listings", {"sort": "nearest"}),
}


def _say(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


async def _tokens(count: int) -> list[str]:
    settings = get_settings()
    async with session_scope() as session:
        rows = await session.execute(
            text(
                "SELECT id FROM users WHERE home_cell_res9 IS NOT NULL AND deleted_at IS NULL "
                "ORDER BY md5(id::text) LIMIT :count"
            ),
            {"count": count},
        )
        tokens = []
        for (user_id,) in rows.all():
            user = await session.get(User, user_id)
            assert user is not None  # noqa: S101
            pair = await auth_service.issue_tokens(session, user=user, settings=settings)
            tokens.append(pair.access_token)
        await session.commit()
    return tokens


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = make_url(settings.database_url).database or ""
    if not database.endswith("_perf"):
        _say(f"Refusing: database {database!r} does not end in _perf.")
        return 1

    statements: dict[str, list[float]] = defaultdict(list)
    if args.statements:
        sync_engine = get_engine().sync_engine

        @event.listens_for(sync_engine, "before_cursor_execute")
        def _start(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
            conn.info.setdefault("started", []).append(time.perf_counter())

        @event.listens_for(sync_engine, "after_cursor_execute")
        def _stop(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
            started = conn.info["started"].pop()
            statements[" ".join(statement.split())[:160]].append(time.perf_counter() - started)

    tokens = await _tokens(args.users + args.warmup)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    timings: dict[str, list[float]] = defaultdict(list)
    failures: dict[str, int] = defaultdict(int)
    async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
        for index, token in enumerate(tokens):
            counted = index >= args.warmup
            headers = {"Authorization": f"Bearer {token}"}
            next_cursor: str | None = None
            for name, (path, extra) in ENDPOINTS.items():
                params = dict(extra)
                if params.pop("page", None):
                    if not next_cursor:
                        continue
                    params["cursor"] = next_cursor
                started = time.perf_counter()
                response = await client.get(path, headers=headers, params=params)
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    failures[name] += 1
                    continue
                if name == "feed":
                    next_cursor = response.json().get("next_cursor")
                if counted:
                    timings[name].append(elapsed)

    _say(f"{args.users} users, {database}")
    _say(f"{'endpoint':<22}{'n':>6}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'max ms':>9}")
    worst = 0.0
    for name in ENDPOINTS:
        values = timings.get(name, [])
        if not values:
            _say(f"{name:<22}{0:>6}  (no successful requests; {failures[name]} failed)")
            continue
        p95 = _percentile(values, 0.95)
        worst = max(worst, p95)
        _say(
            f"{name:<22}{len(values):>6}{statistics.median(values):>9.1f}{p95:>9.1f}"
            f"{_percentile(values, 0.99):>9.1f}{max(values):>9.1f}"
        )
    if any(failures.values()):
        _say(f"failures: {dict(failures)}")

    if args.statements:
        _say("\nSQL by total time:")
        ranked = sorted(statements.items(), key=lambda item: -sum(item[1]))[: args.statements]
        for statement, durations in ranked:
            _say(
                f"{sum(durations) * 1000:>9.0f} ms total  {len(durations):>6}x  "
                f"p50 {statistics.median(durations) * 1000:>6.1f} ms  {statement}"
            )
    await dispose_engine()
    verdict = "meets" if worst < TARGET_P95_MS else "misses"
    _say(f"\nworst p95 {worst:.1f} ms — {verdict} the {TARGET_P95_MS:.0f} ms target")
    return 0 if worst < TARGET_P95_MS else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the feed and the marketplace.")
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--statements", type=int, default=0, help="Show the N slowest statements.")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
