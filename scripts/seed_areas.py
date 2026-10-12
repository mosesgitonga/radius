"""Load area polygons from GeoJSON.

    uv run python -m scripts.seed_areas seeds/areas/nyeri.geojson
    uv run python -m scripts.seed_areas seeds/areas/*.geojson --dry-run

Areas are the human-readable half of the privacy model: a location is only ever
shown as the name of the area containing it. This script is how those names get
into the database, and it is the *only* way — nothing about any particular town is
in the application code, so launching in Karatina or Nanyuki means adding a seed
file, not changing software.

Idempotent, keyed on `(name, town)`, so re-running after editing a boundary
updates it in place rather than creating a second area with the same name.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import cast as sa_cast
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import dispose_engine, session_scope
from app.core.logging import configure_logging, safe_extra
from app.modules.location.models import Area, AreaKind

logger = logging.getLogger("scripts.seed_areas")


@dataclass(frozen=True, slots=True)
class SeedFeature:
    name: str
    kind: str
    town: str | None
    county: str | None
    parent_name: str | None
    geometry: dict[str, Any]


class SeedError(Exception):
    """A seed file that cannot be trusted. Raised rather than skipped: a silently
    dropped area becomes a neighbourhood nobody can be labelled with."""


def _parse_feature(feature: dict[str, Any], index: int) -> SeedFeature:
    properties = feature.get("properties") or {}
    name = (properties.get("name") or "").strip()
    if not name:
        raise SeedError(f"feature {index} has no name")

    kind = (properties.get("kind") or "").strip()
    if kind not in AreaKind.ALL:
        raise SeedError(f"{name!r} has kind {kind!r}; expected one of {', '.join(AreaKind.ALL)}")

    geometry = feature.get("geometry") or {}
    if geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise SeedError(f"{name!r} must be a Polygon or MultiPolygon")

    return SeedFeature(
        name=name,
        kind=kind,
        town=(properties.get("town") or None),
        county=(properties.get("county") or None),
        parent_name=(properties.get("parent") or None),
        geometry=geometry,
    )


def load_features(path: pathlib.Path) -> list[SeedFeature]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("type") != "FeatureCollection":
        raise SeedError(f"{path} is not a GeoJSON FeatureCollection")
    return [
        _parse_feature(feature, index)
        for index, feature in enumerate(document.get("features") or [])
    ]


def _to_multipolygon_geojson(geometry: dict[str, Any]) -> str:
    """Normalise to MultiPolygon.

    The column is MultiPolygon for every area so queries never branch on type,
    and because real administrative boundaries are often discontiguous. A single
    Polygon is wrapped rather than rejected, since that is how most GeoJSON
    arrives.
    """
    if geometry["type"] == "Polygon":
        geometry = {"type": "MultiPolygon", "coordinates": [geometry["coordinates"]]}
    return json.dumps(geometry)


async def _upsert(
    session: AsyncSession, feature: SeedFeature, *, dry_run: bool
) -> tuple[Area | None, str]:
    statement = select(Area).where(Area.name == feature.name, Area.town == feature.town)
    existing = (await session.execute(statement)).scalar_one_or_none()

    # ST_GeomFromGeoJSON yields a geometry; the cast gives the geography the
    # column wants. Doing it in SQL rather than in Python avoids a WKT round trip
    # and lets PostGIS validate the ring order.
    boundary = sa_cast(
        func.ST_Multi(func.ST_GeomFromGeoJSON(_to_multipolygon_geojson(feature.geometry))),
        Geography(geometry_type="MULTIPOLYGON", srid=4326),
    )

    if dry_run:
        return existing, "areas_would_update" if existing else "areas_would_create"

    if existing is None:
        area = Area(
            name=feature.name,
            kind=feature.kind,
            town=feature.town,
            county=feature.county,
            boundary=boundary,
            is_active=True,
        )
        session.add(area)
        await session.flush()
        return area, "areas_created"

    existing.kind = feature.kind
    existing.county = feature.county
    existing.boundary = boundary
    existing.is_active = True
    await session.flush()
    return existing, "areas_updated"


async def seed_file(path: pathlib.Path, *, dry_run: bool) -> dict[str, int]:
    features = load_features(path)
    counts = {
        "areas_created": 0,
        "areas_updated": 0,
        "areas_would_create": 0,
        "areas_would_update": 0,
    }

    async with session_scope() as session:
        by_name: dict[str, Area] = {}

        # Two passes: every area is created first, then parents are linked. A
        # single pass would need the file ordered parents-first, which is a
        # constraint on whoever edits the seed rather than on the software.
        for feature in features:
            area, action = await _upsert(session, feature, dry_run=dry_run)
            counts[action] = counts.get(action, 0) + 1
            if area is not None:
                by_name[feature.name] = area

        if not dry_run:
            for feature in features:
                if feature.parent_name is None:
                    continue
                child = by_name.get(feature.name)
                parent = by_name.get(feature.parent_name)
                if child is None:
                    continue
                if parent is None:
                    # Loud, because a missing parent silently flattens the
                    # fallback chain the labeller walks when a cell is too sparse
                    # to name precisely.
                    logger.warning(
                        "seed_parent_not_found",
                        extra={"area": feature.name, "parent": feature.parent_name},
                    )
                    continue
                child.parent_id = parent.id

    return counts


async def main_async(paths: list[pathlib.Path], *, dry_run: bool) -> int:
    settings = get_settings()
    configure_logging(settings, service="seed_areas")

    total: dict[str, int] = {}
    try:
        for path in paths:
            if not path.exists():
                logger.error("seed_file_missing", extra={"path": str(path)})
                return 2
            counts = await seed_file(path, dry_run=dry_run)
            logger.info(
                "seed_file_loaded",
                extra=safe_extra({"path": path.name, **{k: v for k, v in counts.items() if v}}),
            )
            for key, value in counts.items():
                total[key] = total.get(key, 0) + value
    except SeedError as exc:
        logger.error("seed_invalid", extra={"error": str(exc)})
        return 1
    finally:
        await dispose_engine()

    logger.info("seed_complete", extra=safe_extra({k: v for k, v in total.items() if v}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=pathlib.Path, help="GeoJSON files")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args.paths, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
