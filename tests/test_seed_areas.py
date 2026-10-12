"""The area seeder.

Areas are the only way town data enters the system — nothing about Nyeri is in
application code — so the loader is worth testing as carefully as the code that
reads it. A silently skipped area becomes a neighbourhood nobody can be labelled
with.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.location.models import Area
from scripts.seed_areas import SeedError, load_features, seed_file

SEEDS = pathlib.Path(__file__).resolve().parent.parent / "seeds" / "areas"


def write_collection(path: pathlib.Path, features: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )


def feature(
    name: str,
    *,
    kind: str = "estate",
    parent: str | None = None,
    town: str | None = "Nyeri",
    geometry: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": "Feature",
        "properties": {
            "name": name,
            "kind": kind,
            "town": town,
            "county": "Nyeri",
            "parent": parent,
        },
        "geometry": geometry
        or {
            "type": "Polygon",
            "coordinates": [
                [
                    [36.940, -0.425],
                    [36.950, -0.425],
                    [36.950, -0.415],
                    [36.940, -0.415],
                    [36.940, -0.425],
                ]
            ],
        },
    }


# ---------------------------------------------------------------------------
# The committed Nyeri seed
# ---------------------------------------------------------------------------
def test_the_nyeri_seed_file_parses() -> None:
    features = load_features(SEEDS / "nyeri.geojson")

    assert len(features) >= 10
    assert {f.name for f in features} >= {"Nyeri Town", "Kamakwa", "Karatina"}


def test_the_nyeri_seed_includes_the_launch_anchor() -> None:
    """DeKUT students are the launch community, so the campus has to be nameable."""
    names = {f.name for f in load_features(SEEDS / "nyeri.geojson")}

    assert "Dedan Kimathi University" in names


def test_every_seeded_area_declares_a_known_kind() -> None:
    for parsed in load_features(SEEDS / "nyeri.geojson"):
        assert parsed.kind


def test_the_seed_nests_estates_inside_a_town() -> None:
    """The hierarchy is what the labeller falls back up when a cell is too sparse
    to name precisely."""
    features = {f.name: f for f in load_features(SEEDS / "nyeri.geojson")}

    assert features["Kamakwa"].parent_name == "Nyeri Town"
    assert features["Nyeri Town"].parent_name == "Nyeri County"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
async def test_seeding_creates_areas(db_session: AsyncSession, tmp_path: pathlib.Path) -> None:
    path = tmp_path / "test.geojson"
    write_collection(path, [feature("Testville")])

    counts = await seed_file(path, dry_run=False)

    assert counts["areas_created"] == 1
    created = (await db_session.execute(select(Area).where(Area.name == "Testville"))).scalar_one()
    assert created.town == "Nyeri"
    assert created.is_active is True


async def test_a_polygon_is_stored_as_a_multipolygon(
    db_session: AsyncSession, tmp_path: pathlib.Path
) -> None:
    """One geometry type everywhere, so queries never branch — and real
    administrative boundaries are frequently discontiguous anyway."""
    path = tmp_path / "test.geojson"
    write_collection(path, [feature("Shapeville")])
    await seed_file(path, dry_run=False)

    # Cast to geometry first: ST_GeometryType takes a geometry, and the column
    # is a geography.
    kind = await db_session.scalar(
        select(func.ST_GeometryType(cast(Area.boundary, Geometry))).where(Area.name == "Shapeville")
    )

    assert kind == "ST_MultiPolygon"


async def test_seeding_is_idempotent(db_session: AsyncSession, tmp_path: pathlib.Path) -> None:
    """Re-running after editing a boundary updates it in place, rather than
    creating a second area with the same name."""
    path = tmp_path / "test.geojson"
    write_collection(path, [feature("Twiceville")])

    first = await seed_file(path, dry_run=False)
    second = await seed_file(path, dry_run=False)

    assert first["areas_created"] == 1
    assert second["areas_updated"] == 1
    count = await db_session.scalar(select(func.count(Area.id)).where(Area.name == "Twiceville"))
    assert count == 1


async def test_a_dry_run_changes_nothing(db_session: AsyncSession, tmp_path: pathlib.Path) -> None:
    path = tmp_path / "test.geojson"
    write_collection(path, [feature("Maybeville")])

    counts = await seed_file(path, dry_run=True)

    assert counts["areas_would_create"] == 1
    assert (
        await db_session.scalar(select(func.count(Area.id)).where(Area.name == "Maybeville")) == 0
    )


async def test_parents_link_regardless_of_file_order(
    db_session: AsyncSession, tmp_path: pathlib.Path
) -> None:
    """Two passes, so whoever edits a seed file does not have to order it
    parents-first."""
    path = tmp_path / "test.geojson"
    write_collection(
        path,
        [
            feature("Child Estate", parent="Parent Town"),
            feature("Parent Town", kind="town", town=None),
        ],
    )

    await seed_file(path, dry_run=False)

    child = (await db_session.execute(select(Area).where(Area.name == "Child Estate"))).scalar_one()
    parent = (await db_session.execute(select(Area).where(Area.name == "Parent Town"))).scalar_one()
    assert child.parent_id == parent.id


async def test_a_missing_parent_does_not_abort_the_load(
    db_session: AsyncSession, tmp_path: pathlib.Path
) -> None:
    """Warned about loudly, because it flattens the label fallback chain — but
    the area itself is still worth having."""
    path = tmp_path / "test.geojson"
    write_collection(path, [feature("Orphan Estate", parent="Nowhere Town")])

    await seed_file(path, dry_run=False)

    orphan = (
        await db_session.execute(select(Area).where(Area.name == "Orphan Estate"))
    ).scalar_one()
    assert orphan.parent_id is None


# ---------------------------------------------------------------------------
# Rejecting bad input
# ---------------------------------------------------------------------------
def test_a_non_feature_collection_is_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bad.geojson"
    path.write_text(json.dumps({"type": "Feature"}), encoding="utf-8")

    with pytest.raises(SeedError, match="FeatureCollection"):
        load_features(path)


def test_a_nameless_area_is_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bad.geojson"
    write_collection(path, [feature("")])

    with pytest.raises(SeedError, match="no name"):
        load_features(path)


def test_an_unknown_kind_is_rejected(tmp_path: pathlib.Path) -> None:
    """Raised rather than skipped: an area silently dropped becomes a
    neighbourhood nobody can be labelled with, and nothing would say why."""
    path = tmp_path / "bad.geojson"
    write_collection(path, [feature("Oddville", kind="spaceport")])

    with pytest.raises(SeedError, match="kind"):
        load_features(path)


def test_a_point_geometry_is_rejected(tmp_path: pathlib.Path) -> None:
    """Areas are regions. A point would store but never contain anything, so
    every label inside it would fall back to generic."""
    path = tmp_path / "bad.geojson"
    write_collection(
        path,
        [feature("Pointville", geometry={"type": "Point", "coordinates": [36.94, -0.42]})],
    )

    with pytest.raises(SeedError, match="Polygon"):
        load_features(path)
