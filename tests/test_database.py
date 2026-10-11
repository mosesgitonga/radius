"""The database layer, against a real PostGIS instance.

These tests exist to catch environment problems early: a Postgres without
PostGIS, a missing `pg_trgm`, a geography column that silently behaves as a
planar geometry. Every one of those failures would otherwise show up much later
as a radius that is quietly wrong.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import KARATINA, NYERI_TOWN, offset_km


async def test_postgis_extension_is_available(db_session: AsyncSession) -> None:
    version = await db_session.scalar(text("SELECT postgis_lib_version()"))

    assert version is not None
    # PostGIS 3 is what the brief specifies; the geography functions used for
    # radius queries behave differently enough on 2.x to matter.
    assert int(str(version).split(".")[0]) >= 3


async def test_pg_trgm_extension_is_available(db_session: AsyncSession) -> None:
    """Backs fuzzy name search for groups, listings, and service providers."""
    similarity = await db_session.scalar(text("SELECT similarity('mama fua', 'mama fua laundry')"))

    assert similarity is not None
    assert 0.0 < float(similarity) < 1.0


async def test_geography_distance_matches_expected_metres(
    db_session: AsyncSession,
) -> None:
    """A geography column must measure real distance on the globe.

    Nyeri to Karatina is roughly 22 km. If this came back in degrees, or as a
    planar approximation, every radius in the product would be wrong.
    """
    distance = await db_session.scalar(
        text(
            "SELECT ST_Distance("
            "  ST_SetSRID(ST_MakePoint(:lon_a, :lat_a), 4326)::geography,"
            "  ST_SetSRID(ST_MakePoint(:lon_b, :lat_b), 4326)::geography)"
        ),
        {
            "lon_a": NYERI_TOWN.longitude,
            "lat_a": NYERI_TOWN.latitude,
            "lon_b": KARATINA.longitude,
            "lat_b": KARATINA.latitude,
        },
    )

    assert distance is not None
    assert 20_000 < float(distance) < 24_000


async def test_st_dwithin_selects_by_radius(db_session: AsyncSession) -> None:
    """`ST_DWithin` is the primitive every feed and search query is built on."""
    near = offset_km(NYERI_TOWN, north=2.0)
    far = offset_km(NYERI_TOWN, north=20.0)

    rows = await db_session.execute(
        text(
            "SELECT label FROM (VALUES"
            "  ('near', ST_SetSRID(ST_MakePoint(:near_lon, :near_lat), 4326)::geography),"
            "  ('far',  ST_SetSRID(ST_MakePoint(:far_lon, :far_lat), 4326)::geography)"
            ") AS candidates(label, geog)"
            " WHERE ST_DWithin("
            "   candidates.geog,"
            "   ST_SetSRID(ST_MakePoint(:origin_lon, :origin_lat), 4326)::geography,"
            "   :radius_m)"
        ),
        {
            "near_lon": near.longitude,
            "near_lat": near.latitude,
            "far_lon": far.longitude,
            "far_lat": far.latitude,
            "origin_lon": NYERI_TOWN.longitude,
            "origin_lat": NYERI_TOWN.latitude,
            "radius_m": 5_000,
        },
    )

    assert [row.label for row in rows] == ["near"]


async def test_timestamps_come_back_timezone_aware(db_session: AsyncSession) -> None:
    """Everything is stored as `timestamptz` in UTC.

    A naive timestamp would make event reminders — the one place the product
    works in `Africa/Nairobi` — fire three hours out.
    """
    now = await db_session.scalar(text("SELECT now()"))

    assert now is not None
    assert now.tzinfo is not None
    assert now.utcoffset() is not None


async def test_session_isolation_between_tests(db_session: AsyncSession) -> None:
    """Writes a row that the next test must not see; the pair proves rollback."""
    await db_session.execute(text("CREATE TEMP TABLE isolation_probe (id int)"))
    await db_session.execute(text("INSERT INTO isolation_probe VALUES (1)"))
    await db_session.commit()

    count = await db_session.scalar(text("SELECT count(*) FROM isolation_probe"))
    assert count == 1


async def test_previous_test_left_nothing_behind(db_session: AsyncSession) -> None:
    exists = await db_session.scalar(text("SELECT to_regclass('isolation_probe') IS NOT NULL"))

    assert exists is False
