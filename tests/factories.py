"""Synthetic data helpers.

Location fixtures are the important part. The radius engine has to be tested
against users at *known* distances from each other — dense clusters, sparse
outskirts, and points placed exactly on a boundary — so these helpers build
coordinates by offsetting a known anchor by a given number of kilometres, rather
than hardcoding magic latitudes and longitudes nobody can check.

Anchors are approximate town centres, which is all that is needed and all that
should be in a repository: nothing here is a real person's location.

Model factories arrive with the models themselves in Phase 1.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final

# Metres per degree of latitude, and per degree of longitude at the equator.
# Good to a few parts in a thousand over the distances the radius system uses
# (1-50 km), which is far better than the precision of an H3 res-9 cell.
_METRES_PER_DEGREE_LAT: Final = 110_574.0
_METRES_PER_DEGREE_LON_EQUATOR: Final = 111_320.0


@dataclass(frozen=True, slots=True)
class Coordinates:
    """A latitude/longitude pair, as the client would send it.

    Only ever a test input: the server converts coordinates to an H3 cell on
    receipt and never stores them.
    """

    latitude: float
    longitude: float

    def as_tuple(self) -> tuple[float, float]:
        return self.latitude, self.longitude

    def as_wkt(self) -> str:
        """WKT for handing straight to PostGIS. Note the longitude-first order,
        which is the opposite of how coordinates are usually spoken."""
        return f"POINT({self.longitude} {self.latitude})"


# Approximate anchors for the launch towns. Nyeri is first (the brief's launch
# market), Karatina second. Nothing in the application hardcodes these: new towns
# are added as seed data, and these exist only so tests have fixed reference points.
NYERI_TOWN: Final = Coordinates(-0.4201, 36.9476)
KARATINA: Final = Coordinates(-0.4817, 37.1288)
NANYUKI: Final = Coordinates(0.0167, 37.0725)
OTHAYA: Final = Coordinates(-0.5500, 36.9333)

# The launch anchor community, a few kilometres north of Nyeri town.
DEKUT_CAMPUS: Final = Coordinates(-0.3966, 36.9620)


def offset_km(origin: Coordinates, *, north: float = 0.0, east: float = 0.0) -> Coordinates:
    """A point `north` and `east` kilometres from `origin`.

    Equirectangular approximation, which is more than accurate enough at these
    distances and, unlike a geodesic calculation, is obvious enough to read and
    verify by hand. `tests/test_geo_helpers.py` checks it against PostGIS.
    """
    latitude = origin.latitude + (north * 1000.0) / _METRES_PER_DEGREE_LAT
    metres_per_degree_lon = _METRES_PER_DEGREE_LON_EQUATOR * math.cos(math.radians(origin.latitude))
    longitude = origin.longitude + (east * 1000.0) / metres_per_degree_lon
    return Coordinates(latitude=latitude, longitude=longitude)


def scatter(
    origin: Coordinates,
    *,
    count: int,
    radius_km: float,
    seed: int = 0,
) -> list[Coordinates]:
    """`count` points spread evenly over a disc around `origin`.

    Seeded, so a failing density test fails the same way twice. The square root
    on the radius keeps the distribution uniform by area; without it the points
    bunch towards the centre and a density test measures the wrong thing.
    """
    # Not cryptographic, deliberately: test fixtures must be reproducible.
    rng = random.Random(seed)
    points: list[Coordinates] = []
    for _ in range(count):
        bearing = rng.uniform(0.0, 2.0 * math.pi)
        distance = radius_km * math.sqrt(rng.random())
        points.append(
            offset_km(
                origin,
                north=distance * math.cos(bearing),
                east=distance * math.sin(bearing),
            )
        )
    return points


class SequenceCounter:
    """Monotonic counter for generating unique test values."""

    def __init__(self, start: int = 0) -> None:
        self._value = start

    def next(self) -> int:
        self._value += 1
        return self._value


_phone_counter = SequenceCounter()
_name_counter = SequenceCounter()


def unique_phone() -> str:
    """A unique, structurally valid Kenyan mobile number in E.164 form.

    Uses the 0790-prefix range so test numbers are obviously synthetic and cannot
    collide with a real subscriber if one ever leaks into a log.
    """
    return f"+25479000{_phone_counter.next():04d}"


def unique_display_name(prefix: str = "Test Neighbour") -> str:
    return f"{prefix} {_name_counter.next()}"
