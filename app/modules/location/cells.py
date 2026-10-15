"""H3 cell arithmetic.

Pure functions over coordinates and cells, with no database and no Redis, so the
geometry can be reasoned about and tested on its own. Everything else in the
location module builds on this.

**Why cells at all.** The product needs to answer "who is near me?" without ever
knowing where anyone is. An H3 cell is the compromise: a res-9 cell spans a few
hundred metres, so it is precise enough for a neighbourhood feed and far too
coarse to identify a house. Converting on arrival and discarding the coordinates
means the precise position is never stored, never logged, and never breachable —
it does not exist.

**Why the centroid.** Every proximity query runs against the centre of the cell,
not the position the client sent. Two neighbours in the same cell are therefore
exactly the same distance from everything, which is what makes distance
unexploitable: measuring it repeatedly cannot narrow anyone down below cell size.

**Cells as integers.** H3 indexes are 64-bit integers that the library hands back
as hex strings. They are stored as `BIGINT`: half the size, faster to compare and
index, and it is what an H3 index actually is.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import h3

# H3 resolution 0 is continent-sized and 15 is sub-metre. Guard rails so a bad
# configuration value fails here rather than producing a nonsense radius.
MIN_RESOLUTION: Final = 0
MAX_RESOLUTION: Final = 15


class DistanceBucket(StrEnum):
    """How distance is exposed to clients. Never a number.

    The brief forbids exact distances for a specific reason: an attacker who can
    set their own position and read an exact distance can locate someone's home by
    trilateration in three requests. Buckets make that useless — they are wide
    enough that the answer barely changes as the attacker moves.
    """

    under_1km = "under_1km"
    one_to_3km = "1_3km"
    three_to_10km = "3_10km"
    over_10km = "10km_plus"


# Upper bounds in metres, ascending. The last bucket is unbounded.
_BUCKET_BOUNDS: Final[tuple[tuple[float, DistanceBucket], ...]] = (
    (1_000.0, DistanceBucket.under_1km),
    (3_000.0, DistanceBucket.one_to_3km),
    (10_000.0, DistanceBucket.three_to_10km),
)

BUCKET_BOUNDS: Final = _BUCKET_BOUNDS
"""Public so SQL that sorts by band (the marketplace's "nearest") uses the same
boundaries as the band shown."""
BUCKET_ORDER: Final[tuple[DistanceBucket, ...]] = (
    *(bucket for _, bucket in _BUCKET_BOUNDS),
    DistanceBucket.over_10km,
)


class InvalidCoordinatesError(ValueError):
    """Raised for coordinates outside the valid range for latitude or longitude."""


@dataclass(frozen=True, slots=True)
class CellLocation:
    """A location reduced to cells, which is the only form the system stores.

    Carries the cell at the storage resolution plus its coarser parents, because
    callers need all of them — the parents drive density aggregation and
    k-anonymity, and deriving them later would mean re-importing H3 in the
    database layer.
    """

    cell_res9: int
    cell_res8: int
    cell_res7: int
    cell_res6: int
    centroid_latitude: float
    centroid_longitude: float

    @property
    def centroid_wkt(self) -> str:
        """WKT for PostGIS. Longitude first, which is the opposite of how
        coordinates are usually spoken and a reliable source of bugs."""
        return f"SRID=4326;POINT({self.centroid_longitude} {self.centroid_latitude})"

    def cell_at(self, resolution: int) -> int:
        match resolution:
            case 9:
                return self.cell_res9
            case 8:
                return self.cell_res8
            case 7:
                return self.cell_res7
            case 6:
                return self.cell_res6
            case _:
                raise ValueError(f"no stored cell at resolution {resolution}")


def validate_coordinates(latitude: float, longitude: float) -> None:
    """Reject coordinates that cannot exist.

    H3 would happily accept some of these and return a cell, which would then be
    stored and quietly wrong, so the check happens before conversion.
    """
    if not (-90.0 <= latitude <= 90.0):
        raise InvalidCoordinatesError("latitude must be between -90 and 90")
    if not (-180.0 <= longitude <= 180.0):
        raise InvalidCoordinatesError("longitude must be between -180 and 180")


def to_cell(latitude: float, longitude: float, resolution: int) -> int:
    """One coordinate pair to one cell, as an integer."""
    validate_coordinates(latitude, longitude)
    return int(h3.str_to_int(h3.latlng_to_cell(latitude, longitude, resolution)))


def parent_cell(cell: int, resolution: int) -> int:
    """The containing cell at a coarser resolution."""
    return int(h3.str_to_int(h3.cell_to_parent(h3.int_to_str(cell), resolution)))


def cell_centroid(cell: int) -> tuple[float, float]:
    """The cell's centre, as (latitude, longitude)."""
    latitude, longitude = h3.cell_to_latlng(h3.int_to_str(cell))
    return float(latitude), float(longitude)


def cell_resolution(cell: int) -> int:
    return int(h3.get_resolution(h3.int_to_str(cell)))


def locate(
    latitude: float,
    longitude: float,
    *,
    storage_resolution: int = 9,
) -> CellLocation:
    """Convert coordinates into the stored form, and discard them.

    This is the chokepoint the privacy model depends on: everything downstream
    receives a `CellLocation` and has no way to recover what was passed in. The
    returned centroid is the centre of the res-9 cell, not the input.
    """
    validate_coordinates(latitude, longitude)
    if not MIN_RESOLUTION <= storage_resolution <= MAX_RESOLUTION:
        raise ValueError(f"resolution {storage_resolution} is out of range")

    finest = h3.latlng_to_cell(latitude, longitude, storage_resolution)
    centroid_latitude, centroid_longitude = h3.cell_to_latlng(finest)

    return CellLocation(
        cell_res9=h3.str_to_int(finest),
        cell_res8=h3.str_to_int(h3.cell_to_parent(finest, storage_resolution - 1)),
        cell_res7=h3.str_to_int(h3.cell_to_parent(finest, storage_resolution - 2)),
        cell_res6=h3.str_to_int(h3.cell_to_parent(finest, storage_resolution - 3)),
        centroid_latitude=float(centroid_latitude),
        centroid_longitude=float(centroid_longitude),
    )


def grid_distance(cell_a: int, cell_b: int) -> int:
    """How many rings apart two cells of the same resolution are.

    Used to group density rows by ring after one bulk query, instead of issuing a
    query per ring. Returns -1 when the distance is undefined, which H3 reports
    for cells spanning an icosahedron pentagon; the caller treats that as "too
    far to count" rather than crashing on a geometry edge case.
    """
    try:
        return int(h3.grid_distance(h3.int_to_str(cell_a), h3.int_to_str(cell_b)))
    except Exception:  # pragma: no cover — pentagon or mismatched resolutions
        return -1


def ring(cell: int, k: int) -> list[int]:
    """The cells exactly `k` steps from `cell`. `k=0` is the cell itself."""
    if k < 0:
        raise ValueError("ring distance cannot be negative")
    cells = h3.grid_ring(h3.int_to_str(cell), k)
    return [h3.str_to_int(c) for c in cells]


def disk(cell: int, k: int) -> list[int]:
    """Every cell within `k` steps, inclusive."""
    if k < 0:
        raise ValueError("ring distance cannot be negative")
    return [h3.str_to_int(c) for c in h3.grid_disk(h3.int_to_str(cell), k)]


def ring_distance_km(cell: int, k: int) -> float:
    """How far `k` rings reach from the centre of `cell`, in kilometres.

    Measured from real H3 geometry rather than a hardcoded constant per the brief,
    and it genuinely matters: H3 cells are not uniform. Their size varies with
    latitude and with the icosahedral distortion, so a fixed "1.2 km per ring"
    would be wrong by a different amount in Nyeri than in Nairobi, and the radius
    the user is told would not match the one being queried.

    The centroid-to-centroid distance to the nearest ring member understates the
    reach slightly — the far edge of those cells is further out — so half a cell
    diameter is added to cover the whole ring.
    """
    if k < 0:
        raise ValueError("ring distance cannot be negative")

    origin = h3.int_to_str(cell)
    resolution = h3.get_resolution(origin)
    # Edge length relates to the centre-to-vertex distance; doubling it gives the
    # across-corners diameter, and half of that is the padding to the cell's edge.
    half_diameter_km = float(h3.average_hexagon_edge_length(resolution, unit="km"))

    if k == 0:
        return half_diameter_km

    members = h3.grid_ring(origin, k)
    if not members:  # pragma: no cover — only at the icosahedron's pentagons
        return k * 2 * half_diameter_km

    origin_lat, origin_lng = h3.cell_to_latlng(origin)
    distances = [
        float(
            h3.great_circle_distance((origin_lat, origin_lng), h3.cell_to_latlng(member), unit="km")
        )
        for member in members
    ]
    # The mean, not the maximum: ring members sit at slightly different distances
    # because hexagons tile imperfectly, and the mean is the honest centre of the
    # ring rather than its most distant corner.
    return sum(distances) / len(distances) + half_diameter_km


def distance_bucket(metres: float) -> DistanceBucket:
    """Place a distance into its public bucket."""
    for upper_bound, bucket in _BUCKET_BOUNDS:
        if metres < upper_bound:
            return bucket
    return DistanceBucket.over_10km


def bucket_between_cells(cell_a: int, cell_b: int) -> DistanceBucket:
    """The bucket separating two cells, computed from their centroids.

    Deliberately cell-to-cell rather than point-to-point: the inputs are already
    coarse, so the result cannot be sharpened by repeating the measurement.
    """
    metres = (
        float(
            h3.great_circle_distance(
                h3.cell_to_latlng(h3.int_to_str(cell_a)),
                h3.cell_to_latlng(h3.int_to_str(cell_b)),
                unit="km",
            )
        )
        * 1000.0
    )
    return distance_bucket(metres)
