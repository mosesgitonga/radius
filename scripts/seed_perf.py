"""Fill a performance database with a town's worth of data.

    DATABASE_URL=postgresql+asyncpg://.../myradius_perf uv run python -m scripts.seed_perf

Defaults: 50,000 users, 500,000 posts, 50,000 listings, 10,000 service posts and
5,000 events, all around the centre of the areas seed file given (Nyeri's, by
default — the town is data, not code).

Rows are generated inside PostgreSQL (`INSERT … SELECT generate_series`) from a
pool of real H3 home cells computed here, so 600,000 rows take about a minute
rather than an hour, and every row is shaped exactly as the application writes
it: cells at four resolutions, the cell centre as the centroid, an area resolved
by polygon.

Refuses to run against a database whose name does not end in `_perf`: this
script adds tens of thousands of fake people.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import pathlib
import random
import sys
import time

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.db import dispose_engine, session_scope
from app.modules.location import bands, cells
from app.modules.location.tasks import refresh_cell_activity
from scripts.seed_areas import seed_file

DEFAULT_AREAS = pathlib.Path("seeds/areas/nyeri.geojson")
HOME_CELLS = 6000


def _say(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _centre(path: pathlib.Path) -> tuple[float, float]:
    """The mean of every vertex in the seed file: the middle of the town."""
    data = json.loads(path.read_text(encoding="utf-8"))
    points: list[tuple[float, float]] = []

    def walk(node: object) -> None:
        if (
            isinstance(node, list)
            and len(node) == 2
            and all(isinstance(v, int | float) for v in node)
        ):
            points.append((float(node[1]), float(node[0])))
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for feature in data["features"]:
        walk(feature["geometry"]["coordinates"])
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def _home_cells(
    centre: tuple[float, float], *, spread_km: float, count: int
) -> list[tuple[int, int, int, int, str, str]]:
    """Distinct res-9 home cells, denser towards the middle, as a town is."""
    rng = random.Random(8)  # noqa: S311 — test data, not security
    seen: dict[int, tuple[int, int, int, int, str, str]] = {}
    latitude, longitude = centre
    while len(seen) < count:
        distance = abs(rng.gauss(0, spread_km / 2))
        bearing = rng.uniform(0, 2 * math.pi)
        lat = latitude + (distance / 111.0) * math.cos(bearing)
        lng = longitude + (distance / (111.0 * math.cos(math.radians(latitude)))) * math.sin(
            bearing
        )
        located = cells.locate(lat, lng)
        if located.cell_res9 not in seen:
            seen[located.cell_res9] = (
                located.cell_res9,
                located.cell_res8,
                located.cell_res7,
                located.cell_res6,
                located.centroid_wkt,
                bands.coarse_wkt(located.cell_res8),
            )
    return list(seen.values())


STEPS: list[tuple[str, str]] = [
    (
        "users",
        """
        INSERT INTO users (id, phone_number, display_name, status, phone_verified_at,
            adult_confirmed_at, last_active_at, created_at, updated_at,
            home_cell_res9, home_cell_res8, home_cell_res7, home_cell_res6, home_centroid,
            home_set_at, home_presence_mask, home_presence_updated_on)
        SELECT gen_random_uuid(), '+2547' || lpad(g::text, 8, '0'), 'Neighbour ' || g, 'active',
            now(), now(), now() - random() * interval '20 days',
            now() - random() * interval '365 days', now(),
            c.res9, c.res8, c.res7, c.res6, ST_GeogFromText(c.wkt),
            now() - interval '30 days', (random() * 16383)::int, current_date
        FROM generate_series(1, :users) AS g
        JOIN perf_cells c ON c.n = 1 + (g::bigint * 7919) % :cells
        """,
    ),
    (
        "user areas",
        """
        UPDATE users u SET home_area_id = (
            SELECT a.id FROM areas a
            WHERE a.is_active AND ST_Covers(a.boundary, u.home_centroid)
            ORDER BY ST_Area(a.boundary) ASC
            LIMIT 1
        )
        WHERE u.home_centroid IS NOT NULL
        """,
    ),
    (
        "user index",
        """
        CREATE TEMP TABLE perf_users AS
        SELECT row_number() OVER (ORDER BY u.id) AS n, u.id, u.home_cell_res9 AS res9,
            u.home_cell_res8 AS res8, u.home_cell_res7 AS res7, u.home_cell_res6 AS res6,
            u.home_centroid AS centroid, u.home_area_id AS area_id, c.coarse
        FROM users u JOIN perf_cells c ON c.res9 = u.home_cell_res9
        """,
    ),
    (
        "posts",
        """
        INSERT INTO posts (id, author_id, body, category, created_at, updated_at, deleted_at,
            cell_res9, cell_res8, cell_res7, cell_res6, centroid, area_id)
        SELECT gen_random_uuid(), u.id,
            'Post ' || g || ': has anyone seen the matatu stage moved near the market?',
            (ARRAY['general','question','recommendation','lost_and_found','alert'])[1 + g % 5],
            t, t, CASE WHEN g % 50 = 0 THEN t + interval '1 day' END,
            u.res9, u.res8, u.res7, u.res6, u.centroid, u.area_id
        FROM generate_series(1, :posts) AS g
        JOIN perf_users u ON u.n = 1 + (g::bigint * 104729) % :users
        CROSS JOIN LATERAL (SELECT now() - (g::float / :posts) * interval '90 days' AS t) AS times
        """,
    ),
    (
        "listings",
        """
        INSERT INTO listings (id, seller_id, title, description, category, condition, pricing,
            price_kes, expires_at, content_hash, coarse_centroid, created_at, updated_at,
            cell_res9, cell_res8, cell_res7, cell_res6, centroid, area_id)
        SELECT gen_random_uuid(), u.id, 'Item ' || g || ' — sofa, phone or bicycle',
            'Good condition, collect in town',
            (ARRAY['electronics','phones','furniture','household','clothing_and_shoes'])[1 + g % 5],
            (ARRAY['new','like_new','good','fair'])[1 + g % 4], 'negotiable',
            500 + (g * 37) % 90000,
            now() + (g % 30) * interval '1 day', md5(g::text), ST_GeogFromText(u.coarse),
            now() - (g % 60) * interval '1 day', now(),
            u.res9, u.res8, u.res7, u.res6, u.centroid, u.area_id
        FROM generate_series(1, :listings) AS g
        JOIN perf_users u ON u.n = 1 + (g::bigint * 15485863) % :users
        """,
    ),
    (
        "service posts",
        """
        INSERT INTO service_posts (id, author_id, kind, category, title, description,
            coarse_centroid, expires_at, created_at, updated_at,
            cell_res9, cell_res8, cell_res7, cell_res6, centroid, area_id)
        SELECT gen_random_uuid(), u.id, (ARRAY['offering','seeking','hiring'])[1 + g % 3],
            (ARRAY['plumbing','electrical','laundry','house_help','tutoring'])[1 + g % 5],
            'Service ' || g, 'Reliable and local', ST_GeogFromText(u.coarse),
            CASE WHEN g % 3 = 0 THEN NULL ELSE now() + interval '20 days' END,
            now() - (g % 60) * interval '1 day', now(),
            u.res9, u.res8, u.res7, u.res6, u.centroid, u.area_id
        FROM generate_series(1, :services) AS g
        JOIN perf_users u ON u.n = g
        """,
    ),
    (
        "events",
        """
        INSERT INTO events (id, organiser_id, title, description, event_type, start_at, end_at,
            venue_name, coarse_centroid, visibility, created_at, updated_at,
            cell_res9, cell_res8, cell_res7, cell_res6, centroid, area_id)
        SELECT gen_random_uuid(), u.id, 'Event ' || g, 'All welcome',
            (ARRAY['party','church','sports','harambee','clean_up','meetup'])[1 + g % 6],
            now() + (g % 45) * interval '1 day',
            now() + (g % 45) * interval '1 day' + interval '3 hours',
            'Community hall', ST_GeogFromText(u.coarse), 'public', now(), now(),
            u.res9, u.res8, u.res7, u.res6, u.centroid, u.area_id
        FROM generate_series(1, :events) AS g
        JOIN perf_users u ON u.n = 1 + (g::bigint * 7) % :users
        """,
    ),
]


async def seed(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = make_url(settings.database_url).database or ""
    if not database.endswith("_perf"):
        _say(f"Refusing: database {database!r} does not end in _perf.")
        return 1

    _say(f"seeding areas from {args.areas}")
    await seed_file(args.areas, dry_run=False)

    centre = _centre(args.areas)
    _say(f"computing {HOME_CELLS} home cells around {centre[0]:.4f}, {centre[1]:.4f}")
    pool = _home_cells(centre, spread_km=args.spread_km, count=HOME_CELLS)

    params = {
        "users": args.users,
        "posts": args.posts,
        "listings": args.listings,
        "services": min(args.services, args.users),
        "events": args.events,
        "cells": len(pool),
    }
    async with session_scope() as session:
        await session.execute(
            text(
                "CREATE TEMP TABLE perf_cells (n int PRIMARY KEY, res9 bigint, res8 bigint, "
                "res7 bigint, res6 bigint, wkt text, coarse text) ON COMMIT DROP"
            )
        )
        await session.execute(
            text("INSERT INTO perf_cells VALUES (:n, :res9, :res8, :res7, :res6, :wkt, :coarse)"),
            [
                {
                    "n": n,
                    "res9": c[0],
                    "res8": c[1],
                    "res7": c[2],
                    "res6": c[3],
                    "wkt": c[4],
                    "coarse": c[5],
                }
                for n, c in enumerate(pool, start=1)
            ],
        )
        for label, sql in STEPS:
            started = time.perf_counter()
            result = await session.execute(text(sql), params)
            rows = getattr(result, "rowcount", -1)
            _say(f"  {label}: {rows} rows in {time.perf_counter() - started:.1f}s")
        await session.commit()

    async with session_scope() as session:
        await session.execute(text("ANALYZE"))
        await refresh_cell_activity(session, settings=settings)
        await session.commit()
    await dispose_engine()
    _say("done")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a performance database.")
    parser.add_argument("--areas", type=pathlib.Path, default=DEFAULT_AREAS)
    parser.add_argument("--users", type=int, default=50_000)
    parser.add_argument("--posts", type=int, default=500_000)
    parser.add_argument("--listings", type=int, default=50_000)
    parser.add_argument("--services", type=int, default=10_000)
    parser.add_argument("--events", type=int, default=5_000)
    parser.add_argument("--spread-km", type=float, default=8.0)
    return asyncio.run(seed(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
