"""Checks the test helpers themselves against PostGIS.

Later phases assert things like "a user 3 km away is inside the radius and one
at 18 km is outside". Those assertions are only meaningful if `offset_km` really
places a point at the distance it claims, so it is verified against the same
geography maths the production queries use.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import DEKUT_CAMPUS, NYERI_TOWN, Coordinates, offset_km, scatter


async def _distance_metres(session: AsyncSession, a: Coordinates, b: Coordinates) -> float:
    distance = await session.scalar(
        text(
            "SELECT ST_Distance("
            "  ST_SetSRID(ST_MakePoint(:lon_a, :lat_a), 4326)::geography,"
            "  ST_SetSRID(ST_MakePoint(:lon_b, :lat_b), 4326)::geography)"
        ),
        {
            "lon_a": a.longitude,
            "lat_a": a.latitude,
            "lon_b": b.longitude,
            "lat_b": b.latitude,
        },
    )
    assert distance is not None
    return float(distance)


@pytest.mark.parametrize("kilometres", [0.5, 1.0, 5.0, 15.0, 40.0])
async def test_northward_offset_is_accurate(db_session: AsyncSession, kilometres: float) -> None:
    moved = offset_km(NYERI_TOWN, north=kilometres)

    measured = await _distance_metres(db_session, NYERI_TOWN, moved)

    # Within 0.5%: the equirectangular approximation is far more precise than the
    # few hundred metres an H3 resolution-9 cell spans, which is the real floor
    # on location accuracy in this product.
    assert measured == pytest.approx(kilometres * 1000.0, rel=0.005)


@pytest.mark.parametrize("kilometres", [0.5, 1.0, 5.0, 15.0, 40.0])
async def test_eastward_offset_is_accurate(db_session: AsyncSession, kilometres: float) -> None:
    moved = offset_km(NYERI_TOWN, east=kilometres)

    measured = await _distance_metres(db_session, NYERI_TOWN, moved)

    assert measured == pytest.approx(kilometres * 1000.0, rel=0.005)


async def test_diagonal_offset_is_accurate(db_session: AsyncSession) -> None:
    """3 km north and 4 km east should land 5 km away."""
    moved = offset_km(NYERI_TOWN, north=3.0, east=4.0)

    measured = await _distance_metres(db_session, NYERI_TOWN, moved)

    assert measured == pytest.approx(5_000.0, rel=0.005)


async def test_dekut_is_a_short_hop_from_nyeri_town(db_session: AsyncSession) -> None:
    """Sanity-check the launch anchors.

    DeKUT is the anchor community and Nyeri town is the reference point; they
    need to be close enough that a feed radius plausibly spans both, since the
    density tests in Phase 1 depend on that.
    """
    measured = await _distance_metres(db_session, NYERI_TOWN, DEKUT_CAMPUS)

    assert 1_000 < measured < 10_000


async def test_scatter_stays_within_its_radius(db_session: AsyncSession) -> None:
    points = scatter(NYERI_TOWN, count=25, radius_km=3.0, seed=7)

    distances = [await _distance_metres(db_session, NYERI_TOWN, p) for p in points]

    assert len(points) == 25
    assert max(distances) <= 3_000 * 1.005


async def test_scatter_is_deterministic() -> None:
    """A density test that fails must fail the same way on a rerun."""
    first = scatter(NYERI_TOWN, count=10, radius_km=2.0, seed=42)
    second = scatter(NYERI_TOWN, count=10, radius_km=2.0, seed=42)
    different_seed = scatter(NYERI_TOWN, count=10, radius_km=2.0, seed=43)

    assert first == second
    assert first != different_seed


async def test_scatter_spreads_by_area_not_by_radius(db_session: AsyncSession) -> None:
    """Points should be uniform over the disc, not bunched at the centre.

    Uniform-by-area means about half the points fall outside r/sqrt(2), which is
    ~70% of the radius. Sampling the radius uniformly instead would put ~70% of
    them inside that circle and make every density measurement wrong.
    """
    radius_km = 4.0
    points = scatter(NYERI_TOWN, count=200, radius_km=radius_km, seed=11)

    distances = [await _distance_metres(db_session, NYERI_TOWN, p) for p in points]
    beyond_half_area = sum(1 for d in distances if d > radius_km * 1000 / 2**0.5)

    assert 0.35 < beyond_half_area / len(distances) < 0.65
