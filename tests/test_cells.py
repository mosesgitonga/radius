"""H3 cell arithmetic.

Pure functions, so no database. These are the foundation of both the radius
engine and the privacy model, and the privacy-relevant properties — that a
centroid loses the original position, and that a bucket cannot be sharpened — are
asserted here rather than left implicit.
"""

from __future__ import annotations

import pytest

from app.modules.location import cells
from app.modules.location.cells import DistanceBucket, InvalidCoordinatesError
from tests.factories import KARATINA, NAIROBI, NYERI_TOWN, offset_km


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def test_locate_returns_the_whole_parent_chain() -> None:
    location = cells.locate(*NYERI_TOWN.as_tuple())

    assert cells.parent_cell(location.cell_res9, 8) == location.cell_res8
    assert cells.parent_cell(location.cell_res9, 7) == location.cell_res7
    assert cells.parent_cell(location.cell_res9, 6) == location.cell_res6


def test_cells_are_integers_not_hex_strings() -> None:
    """Stored as BIGINT: half the size of the hex form, faster to index, and what
    an H3 index actually is."""
    location = cells.locate(*NYERI_TOWN.as_tuple())

    assert isinstance(location.cell_res9, int)
    assert location.cell_res9 > 0


def test_resolutions_are_what_was_asked_for() -> None:
    location = cells.locate(*NYERI_TOWN.as_tuple())

    assert cells.cell_resolution(location.cell_res9) == 9
    assert cells.cell_resolution(location.cell_res8) == 8
    assert cells.cell_resolution(location.cell_res6) == 6


def test_the_centroid_is_the_cell_centre_not_the_input() -> None:
    """The heart of the privacy design: what comes back is the cell's centre, so
    the position that went in is not recoverable from it."""
    location = cells.locate(*NYERI_TOWN.as_tuple())

    assert location.centroid_latitude != NYERI_TOWN.latitude
    assert location.centroid_longitude != NYERI_TOWN.longitude
    # Still within a few hundred metres, i.e. the same neighbourhood.
    assert abs(location.centroid_latitude - NYERI_TOWN.latitude) < 0.01
    assert abs(location.centroid_longitude - NYERI_TOWN.longitude) < 0.01


def test_nearby_positions_collapse_to_one_cell() -> None:
    """Two neighbours a hundred metres apart become indistinguishable, which is
    what stops distance being used to single anybody out."""
    here = cells.locate(*NYERI_TOWN.as_tuple())
    next_door = cells.locate(*offset_km(NYERI_TOWN, north=0.02).as_tuple())

    assert here.cell_res9 == next_door.cell_res9
    assert here.centroid_latitude == next_door.centroid_latitude


def test_distant_positions_do_not_share_a_cell() -> None:
    here = cells.locate(*NYERI_TOWN.as_tuple())
    away = cells.locate(*offset_km(NYERI_TOWN, north=5.0).as_tuple())

    assert here.cell_res9 != away.cell_res9
    assert here.cell_res8 != away.cell_res8


def test_the_wkt_puts_longitude_first() -> None:
    """The order PostGIS wants, and the opposite of how coordinates are spoken —
    a reliable source of silently-wrong geography."""
    location = cells.locate(*NYERI_TOWN.as_tuple())

    assert location.centroid_wkt.startswith("SRID=4326;POINT(36.9")


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(91.0, 36.0), (-91.0, 36.0), (0.0, 181.0), (0.0, -181.0)],
)
def test_impossible_coordinates_are_rejected(latitude: float, longitude: float) -> None:
    """H3 would accept some of these and return a cell, which would then be
    stored and quietly wrong."""
    with pytest.raises(InvalidCoordinatesError):
        cells.locate(latitude, longitude)


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(90.0, 180.0), (-90.0, -180.0), (0.0, 0.0)],
)
def test_extreme_but_valid_coordinates_are_accepted(latitude: float, longitude: float) -> None:
    assert cells.locate(latitude, longitude).cell_res9 > 0


def test_cell_at_rejects_an_unstored_resolution() -> None:
    """Only 9, 8, 7 and 6 are stored; asking for another is a bug, not a default."""
    location = cells.locate(*NYERI_TOWN.as_tuple())

    with pytest.raises(ValueError, match="resolution"):
        location.cell_at(5)


# ---------------------------------------------------------------------------
# Rings
# ---------------------------------------------------------------------------
def test_ring_zero_is_the_cell_itself() -> None:
    cell = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8

    assert cells.ring(cell, 0) == [cell]


def test_ring_sizes_grow_by_six() -> None:
    """A hexagonal grid property. If this breaks, the density walk is counting
    the wrong cells."""
    cell = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8

    assert [len(cells.ring(cell, k)) for k in range(4)] == [1, 6, 12, 18]


def test_disk_sizes_are_centred_hexagonal_numbers() -> None:
    cell = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8

    assert [len(cells.disk(cell, k)) for k in range(4)] == [1, 7, 19, 37]


def test_ring_reach_increases_with_k() -> None:
    cell = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8

    reaches = [cells.ring_distance_km(cell, k) for k in range(0, 20, 4)]

    assert reaches == sorted(reaches)


def test_ring_reach_is_roughly_a_kilometre_per_ring_at_res_8() -> None:
    """Not a hardcoded constant in the engine — this test documents the scale so
    that the `radius_max_ring_k` safety bound can be sanity-checked against the
    largest configured maximum."""
    cell = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8

    assert 8.0 < cells.ring_distance_km(cell, 10) < 11.0
    assert 30.0 < cells.ring_distance_km(cell, 40) < 42.0


@pytest.mark.parametrize("k", [-1, -5])
def test_negative_ring_distances_are_rejected(k: int) -> None:
    cell = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8

    with pytest.raises(ValueError, match="negative"):
        cells.ring(cell, k)


def test_grid_distance_matches_the_ring_a_cell_came_from() -> None:
    """The engine groups a bulk density read back into rings with this, so the two
    have to agree exactly or counts land in the wrong ring."""
    anchor = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8

    for k in range(5):
        for member in cells.ring(anchor, k):
            assert cells.grid_distance(anchor, member) == k


# ---------------------------------------------------------------------------
# Distance buckets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("metres", "expected"),
    [
        (0, DistanceBucket.under_1km),
        (999, DistanceBucket.under_1km),
        (1_000, DistanceBucket.one_to_3km),
        (2_999, DistanceBucket.one_to_3km),
        (3_000, DistanceBucket.three_to_10km),
        (9_999, DistanceBucket.three_to_10km),
        (10_000, DistanceBucket.over_10km),
        (500_000, DistanceBucket.over_10km),
    ],
)
def test_bucket_boundaries(metres: float, expected: DistanceBucket) -> None:
    assert cells.distance_bucket(metres) == expected


def test_buckets_between_known_places() -> None:
    nyeri = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8
    near = cells.locate(*offset_km(NYERI_TOWN, north=0.6).as_tuple()).cell_res8
    middling = cells.locate(*offset_km(NYERI_TOWN, north=5.0).as_tuple()).cell_res8
    karatina = cells.locate(*KARATINA.as_tuple()).cell_res8
    nairobi = cells.locate(*NAIROBI.as_tuple()).cell_res8

    assert cells.bucket_between_cells(nyeri, near) == DistanceBucket.under_1km
    assert cells.bucket_between_cells(nyeri, middling) == DistanceBucket.three_to_10km
    assert cells.bucket_between_cells(nyeri, karatina) == DistanceBucket.over_10km
    assert cells.bucket_between_cells(nyeri, nairobi) == DistanceBucket.over_10km


def test_a_bucket_is_symmetric() -> None:
    a = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8
    b = cells.locate(*KARATINA.as_tuple()).cell_res8

    assert cells.bucket_between_cells(a, b) == cells.bucket_between_cells(b, a)


def test_a_bucket_cannot_be_sharpened_by_moving_within_a_cell() -> None:
    """The trilateration defence, stated as a test.

    An attacker who moves a short distance and measures again gets the identical
    answer, because both positions collapse to the same cell. Without this, three
    measurements from three spoofed positions locate someone's home.
    """
    target = cells.locate(*offset_km(NYERI_TOWN, north=2.0).as_tuple()).cell_res8

    readings = {
        cells.bucket_between_cells(
            cells.locate(*offset_km(NYERI_TOWN, east=metres / 1000).as_tuple()).cell_res8,
            target,
        )
        for metres in (0, 20, 50, 100)
    }

    assert len(readings) == 1
