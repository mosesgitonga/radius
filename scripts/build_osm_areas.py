"""Build an area seed file from OpenStreetMap.

    uv run python -m scripts.build_osm_areas --county Nyeri --out seeds/areas/nyeri.geojson

Fetches, once, the county boundary, its wards and the built-up areas of its towns
from the Overpass API. It assembles each boundary into a polygon in PostGIS (so
`DATABASE_URL` must point at a database with PostGIS) and writes a GeoJSON file
that `scripts.seed_areas` loads. Nothing here runs in the application, and the
application never calls OpenStreetMap.

The data is © OpenStreetMap contributors, available under the Open Database
Licence (ODbL). The file records that, and the app credits it (DECISIONS M.10).

Wards are the finest level OpenStreetMap has for Nyeri. Estates can be added to
the file by hand as further features; the loader treats them alike.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import re
import time
from typing import Any

import httpx
from sqlalchemy import text

from app.core.db import dispose_engine, session_scope

logger = logging.getLogger("scripts.build_osm_areas")

OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
SIMPLIFY_DEGREES = 0.0002  # about 20 m: plenty for labels, a small file


def _query(county: str) -> str:
    return f"""[out:json][timeout:180];
area["boundary"="administrative"]["admin_level"="4"]["name"="{county}"]->.county;
(
  relation["boundary"="administrative"]["admin_level"="4"]["name"="{county}"];
  relation["boundary"="administrative"]["admin_level"~"^(6|8)$"](area.county);
  way["landuse"="residential"]["name"](area.county);
  nwr["amenity"="university"]["name"](area.county);
);
out geom;"""


def fetch(county: str, *, rounds: int = 4) -> dict[str, Any]:
    """The raw Overpass answer. Public mirrors are often busy, so each is tried a
    few times with a pause between rounds."""
    last: str = ""
    for attempt in range(rounds):
        if attempt:
            time.sleep(20 * attempt)
        found = _fetch_once(county)
        if isinstance(found, dict):
            return found
        last = found
    raise SystemExit(f"No Overpass mirror answered ({last}). Try again later.")


def _fetch_once(county: str) -> dict[str, Any] | str:
    last: str = ""
    for url in OVERPASS_MIRRORS:
        try:
            response = httpx.post(
                url,
                data={"data": _query(county)},
                headers={"User-Agent": "MyRadius seed builder", "Accept": "application/json"},
                timeout=240,
            )
        except httpx.HTTPError as error:
            last = f"{url}: {error}"
            continue
        if response.status_code == 200 and response.text.lstrip().startswith("{"):
            return dict(response.json())
        last = f"{url}: HTTP {response.status_code}"
    return last


def _lines(element: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """The outer boundary of a way or relation, as lines of (lon, lat)."""
    if element["type"] == "way":
        return [[(p["lon"], p["lat"]) for p in element.get("geometry", [])]]
    if element["type"] == "node":
        return []
    return [
        [(p["lon"], p["lat"]) for p in member.get("geometry", [])]
        for member in element.get("members", [])
        if member.get("type") == "way" and member.get("role") in ("outer", "")
    ]


def _clean_ward(name: str) -> str:
    """'kamakwa/mukaro ward' -> 'Kamakwa/Mukaro'; apostrophes and slashes kept."""
    name = re.sub(r"\s+ward$", "", name.strip(), flags=re.IGNORECASE)
    return "/".join(
        " ".join(word[:1].upper() + word[1:] for word in part.split()) for part in name.split("/")
    )


async def _polygon(session: Any, lines: list[list[tuple[float, float]]]) -> dict[str, Any] | None:
    """Stitch boundary lines into a simplified MultiPolygon, in PostGIS."""
    wkts = [
        "LINESTRING(" + ",".join(f"{lon} {lat}" for lon, lat in line) + ")"
        for line in lines
        if len(line) >= 2
    ]
    if not wkts:
        return None
    row = (
        await session.execute(
            text(
                """
                WITH lines AS (
                    SELECT ST_GeomFromText(w, 4326) AS g FROM unnest(CAST(:wkts AS text[])) AS w
                ),
                noded AS (SELECT ST_Node(ST_Collect(g)) AS g FROM lines),
                faces AS (SELECT (ST_Dump(ST_Polygonize(g))).geom AS g FROM noded),
                merged AS (SELECT ST_Union(g) AS g FROM faces)
                SELECT ST_AsGeoJSON(
                    ST_Multi(ST_CollectionExtract(
                        ST_MakeValid(ST_SimplifyPreserveTopology(g, :tolerance)), 3)),
                    6)
                FROM merged WHERE g IS NOT NULL
                """
            ),
            {"wkts": wkts, "tolerance": SIMPLIFY_DEGREES},
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    geometry = dict(json.loads(row))
    return geometry if geometry.get("coordinates") else None


async def _contains_share(session: Any, outer: dict[str, Any], inner: dict[str, Any]) -> float:
    """How much of `inner` lies inside `outer`, from 0 to 1."""
    return float(
        await session.scalar(
            text(
                """
                SELECT ST_Area(ST_Intersection(a, b)) / NULLIF(ST_Area(b), 0)
                FROM (SELECT ST_GeomFromGeoJSON(:outer) AS a, ST_GeomFromGeoJSON(:inner) AS b) s
                """
            ),
            {"outer": json.dumps(outer), "inner": json.dumps(inner)},
        )
        or 0.0
    )


async def build(county: str, data: dict[str, Any]) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    async with session_scope() as session:
        county_name = f"{county} County"
        sub_counties: list[tuple[str, dict[str, Any]]] = []
        towns: list[tuple[str, dict[str, Any]]] = []
        wards: list[tuple[str, dict[str, Any]]] = []
        campuses: list[tuple[str, dict[str, Any]]] = []
        county_geometry: dict[str, Any] | None = None

        for element in data["elements"]:
            tags = element.get("tags", {})
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            geometry = await _polygon(session, _lines(element))
            if geometry is None:
                logger.warning("osm_area_skipped: %s has no closed boundary", name)
                continue
            level = tags.get("admin_level")
            if tags.get("amenity") == "university":
                campuses.append((name, geometry))
            elif tags.get("landuse") == "residential":
                towns.append((name, geometry))
            elif level == "4":
                county_geometry = geometry
            elif level == "6":
                sub_counties.append((name, geometry))
            elif level == "8":
                wards.append((_clean_ward(name), geometry))

        if county_geometry is None:
            raise SystemExit(f"OpenStreetMap returned no boundary for {county!r}")
        features.append(_feature(county_name, "county", None, county, None, county_geometry))

        town_names: dict[str, str] = {}
        for name, geometry in towns:
            label = f"{name} Town" if name == county else name
            town_names[name] = label
            features.append(_feature(label, "town", None, county, county_name, geometry))

        ward_towns: list[tuple[str, str, dict[str, Any]]] = []
        for name, geometry in wards:
            # The town a ward belongs to, for "town only" labels (decision C.6): the
            # built-up area it overlaps most, else its sub-county.
            # Overlap either way: a small ward inside the town, or a large ward
            # holding much of it.
            best_town, best_share = None, 0.0
            for town, town_geometry in towns:
                share = max(
                    await _contains_share(session, geometry, town_geometry),
                    await _contains_share(session, town_geometry, geometry),
                )
                if share > best_share:
                    best_town, best_share = town, share
            if name.endswith(" Town"):
                town = name.removesuffix(" Town")
            elif best_town is not None and best_share >= 0.2:
                town = best_town
            else:
                town = county
                for sub, sub_geometry in sub_counties:
                    if await _contains_share(session, sub_geometry, geometry) >= 0.5:
                        # "Nyeri Central" is the sub-county around Nyeri, not a town.
                        town = county if sub.startswith(county) else sub
                        break
            ward_towns.append((name, town, geometry))
            features.append(_feature(name, "ward", town, county, county_name, geometry))

        ward_names = {name for name, _, _ in ward_towns}
        for name, geometry in campuses:
            if name in ward_names:
                # A second outline named only after the ward it is in.
                continue
            # A campus belongs to the town of the ward holding most of it.
            campus_town, best = county, 0.0
            for _, town, ward_geometry in ward_towns:
                share = await _contains_share(session, ward_geometry, geometry)
                if share > best:
                    campus_town, best = town, share
            features.append(_feature(name, "campus", campus_town, county, county_name, geometry))

    return {
        "type": "FeatureCollection",
        "name": county.lower(),
        "metadata": {
            "description": (
                f"Areas of {county} County from OpenStreetMap: the county, its wards and the "
                "built-up areas of its towns. Generated by scripts/build_osm_areas.py."
            ),
            "attribution": ATTRIBUTION,
            "licence": "ODbL-1.0",
            "crs": "EPSG:4326",
        },
        "features": features,
    }


def _feature(
    name: str,
    kind: str,
    town: str | None,
    county: str,
    parent: str | None,
    geometry: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "name": name,
            "kind": kind,
            "town": town,
            "county": county,
            "parent": parent,
            "source": "OpenStreetMap",
        },
        "geometry": geometry,
    }


async def build_and_close(county: str, data: dict[str, Any]) -> dict[str, Any]:
    try:
        return await build(county, data)
    finally:
        await dispose_engine()


def run(county: str, out: pathlib.Path, raw: pathlib.Path | None) -> None:
    if raw is not None and raw.exists():
        data = json.loads(raw.read_text(encoding="utf-8"))
    else:
        data = fetch(county)
        if raw is not None:
            raw.write_text(json.dumps(data), encoding="utf-8")
    document = asyncio.run(build_and_close(county, data))
    out.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    kinds: dict[str, int] = {}
    for feature in document["features"]:
        kinds[feature["properties"]["kind"]] = kinds.get(feature["properties"]["kind"], 0) + 1
    logger.info("wrote %s: %s", out, kinds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--county", required=True, help="The county's name in OpenStreetMap")
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument(
        "--raw",
        type=pathlib.Path,
        help="Keep the downloaded OpenStreetMap answer here, and reuse it if it exists",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(args.county, args.out, args.raw)


if __name__ == "__main__":
    main()
