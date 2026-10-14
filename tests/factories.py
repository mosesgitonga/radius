"""Synthetic data helpers.

Location fixtures are the important part. The radius engine has to be tested
against users at *known* distances from each other — dense clusters, sparse
outskirts, and points placed exactly on a boundary — so these helpers build
coordinates by offsetting a known anchor by a given number of kilometres, rather
than hardcoding magic latitudes and longitudes nobody can check.

Anchors are approximate town centres, which is all that is needed and all that
should be in a repository: nothing here is a real person's location.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.providers.storage import StorageProvider
from app.modules.feed import service as feed_service
from app.modules.feed.models import Post
from app.modules.location import cells
from app.modules.location.models import Area, CellActivity
from app.modules.media import service as media_service
from app.modules.media.models import MediaAsset, MediaPurpose
from app.modules.users.models import User, UserStatus

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

    def as_payload(self) -> dict[str, float]:
        """Shaped for `LocationIn`."""
        return {"latitude": self.latitude, "longitude": self.longitude}


# Approximate anchors for the launch towns. Nyeri is first (the brief's launch
# market), Karatina second. Nothing in the application hardcodes these: new towns
# are added as seed data, and these exist only so tests have fixed reference points.
NYERI_TOWN: Final = Coordinates(-0.4201, 36.9476)
KARATINA: Final = Coordinates(-0.4817, 37.1288)
NANYUKI: Final = Coordinates(0.0167, 37.0725)
OTHAYA: Final = Coordinates(-0.5500, 36.9333)

# The launch anchor community, a few kilometres north of Nyeri town.
DEKUT_CAMPUS: Final = Coordinates(-0.3966, 36.9620)

# Far enough to be outside every configured radius, for "a user far away" cases.
NAIROBI: Final = Coordinates(-1.2864, 36.8172)


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


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------
async def make_user(
    session: AsyncSession,
    *,
    at: Coordinates | None = None,
    display_name: str | None = None,
    phone_number: str | None = None,
    status: UserStatus = UserStatus.active,
    last_active_at: datetime | None = None,
    is_verified_neighbour: bool = False,
    storage_resolution: int = 9,
    created_days_ago: float | None = None,
) -> User:
    """A user, optionally with a home location already set.

    Writes the cells directly rather than going through the location service,
    because most tests want a user *at* a place, not a test of how they got there.
    The handful of tests that do care about that path call the service.
    """
    now = datetime.now(UTC)
    user = User(
        phone_number=phone_number or unique_phone(),
        display_name=display_name or unique_display_name(),
        phone_verified_at=now,
        adult_confirmed_at=now,
        last_active_at=last_active_at or now,
        status=status,
        is_verified_neighbour=is_verified_neighbour,
    )
    if created_days_ago is not None:
        # Account age drives new-account rate limits and reporter trust, so tests
        # need to be able to say how old an account is.
        user.created_at = now - timedelta(days=created_days_ago)

    if at is not None:
        location = cells.locate(at.latitude, at.longitude, storage_resolution=storage_resolution)
        user.home_cell_res9 = location.cell_res9
        user.home_cell_res8 = location.cell_res8
        user.home_cell_res7 = location.cell_res7
        user.home_cell_res6 = location.cell_res6
        user.home_centroid = location.centroid_wkt
        user.home_set_at = now

    session.add(user)
    await session.flush()
    return user


async def make_users_at(
    session: AsyncSession,
    *,
    origin: Coordinates,
    count: int,
    radius_km: float,
    seed: int = 0,
    active: bool = True,
) -> list[User]:
    """A cluster of users around a point, for density tests.

    `active=False` places them outside the active window, so a test can prove the
    radius counts active neighbours rather than registered ones.
    """
    stale = datetime.now(UTC) - timedelta(days=400)
    points = scatter(origin, count=count, radius_km=radius_km, seed=seed)
    return [
        await make_user(session, at=point, last_active_at=None if active else stale)
        for point in points
    ]


async def set_cell_density(
    session: AsyncSession,
    *,
    origin: Coordinates,
    active_per_cell: int,
    rings: int,
    registered_per_cell: int | None = None,
    resolution: int = 8,
) -> int:
    """Populate `cell_activity` directly around a point.

    Faster than creating thousands of users when the test is about the radius
    algorithm rather than about how the counts were derived. Tests of the refresh
    worker itself create real users instead.

    Returns the anchor cell.
    """
    anchor = cells.locate(origin.latitude, origin.longitude).cell_at(resolution)
    registered = active_per_cell if registered_per_cell is None else registered_per_cell
    for k in range(rings + 1):
        for cell in cells.ring(anchor, k):
            session.add(
                CellActivity(
                    resolution=resolution,
                    cell=cell,
                    active_user_count=active_per_cell,
                    registered_user_count=max(registered, active_per_cell),
                )
            )
    await session.flush()
    return anchor


async def set_registered_density(
    session: AsyncSession,
    *,
    at: Coordinates,
    registered: int,
    resolutions: tuple[int, ...] = (8, 7, 6),
) -> None:
    """Satisfy k-anonymity for a point, so an area label can be resolved.

    Area labels read registered counts from `cell_activity`, so a test that wants
    a precise label has to say how many neighbours share the cell.
    """
    location = cells.locate(at.latitude, at.longitude)
    for resolution in resolutions:
        session.add(
            CellActivity(
                resolution=resolution,
                cell=location.cell_at(resolution),
                active_user_count=min(registered, registered),
                registered_user_count=registered,
            )
        )
    await session.flush()


async def make_area(
    session: AsyncSession,
    *,
    name: str,
    centre: Coordinates,
    width_km: float = 2.0,
    kind: str = "estate",
    town: str | None = "Nyeri",
    county: str | None = "Nyeri",
    parent_id: uuid.UUID | None = None,
) -> Area:
    """A square area around a point."""
    half_lat = (width_km * 500.0) / _METRES_PER_DEGREE_LAT
    half_lon = (width_km * 500.0) / (
        _METRES_PER_DEGREE_LON_EQUATOR * math.cos(math.radians(centre.latitude))
    )
    west, east = centre.longitude - half_lon, centre.longitude + half_lon
    south, north = centre.latitude - half_lat, centre.latitude + half_lat
    wkt = (
        "SRID=4326;MULTIPOLYGON((("
        f"{west} {south},{east} {south},{east} {north},{west} {north},{west} {south}"
        ")))"
    )
    area = Area(
        name=name,
        kind=kind,
        town=town,
        county=county,
        boundary=wkt,
        parent_id=parent_id,
    )
    session.add(area)
    await session.flush()
    return area


def phone_of(user: User) -> str:
    """A user's number, with the "every live account has one" invariant asserted.

    `User.phone_number` is nullable because a purged account holds none, so this
    narrows the type for the tests that sign somebody in — and fails loudly rather
    than passing None to an endpoint if that invariant ever breaks.
    """
    assert user.phone_number is not None, "a live user must have a phone number"
    return user.phone_number


# ---------------------------------------------------------------------------
# Phase 2: images, media, posts
# ---------------------------------------------------------------------------
def jpeg_with_gps(
    *, width: int = 1600, height: int = 1200, latitude: float = -0.42, longitude: float = 36.95
) -> bytes:
    """A JPEG carrying the metadata a real phone photo would.

    GPS coordinates, camera make and model, and a capture timestamp — everything
    the pipeline must remove. Built here rather than checked in as a fixture file,
    so the test states exactly what it is proving is stripped.
    """
    import io
    from fractions import Fraction

    from PIL import Image

    def dms(value: float) -> tuple[Fraction, Fraction, Fraction]:
        value = abs(value)
        degrees = int(value)
        minutes = int((value - degrees) * 60)
        seconds = round(((value - degrees) * 60 - minutes) * 60, 2)
        return Fraction(degrees), Fraction(minutes), Fraction(seconds).limit_denominator(100)

    image = Image.new("RGB", (width, height), (120, 160, 90))
    exif = image.getexif()
    exif[271] = "TestCamera"
    exif[272] = "Model X"
    exif[306] = "2026:10:12 14:30:00"
    gps = exif.get_ifd(34853)
    gps[1] = "S" if latitude < 0 else "N"
    gps[2] = dms(latitude)
    gps[3] = "W" if longitude < 0 else "E"
    gps[4] = dms(longitude)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif, quality=90)
    return buffer.getvalue()


def png_bytes(width: int = 400, height: int = 300, *, mode: str = "RGB") -> bytes:
    import io

    from PIL import Image

    image = Image.new(mode, (width, height))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def make_ready_image(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    owner: User,
    purpose: MediaPurpose = MediaPurpose.post,
    data: bytes | None = None,
) -> MediaAsset:
    """An upload taken all the way through the pipeline to `ready`.

    Uses the real service functions, so a test that attaches an image to a post is
    also exercising the path a real image takes.
    """
    settings = get_settings()
    ticket = await media_service.create_upload(
        session,
        storage,
        owner_id=owner.id,
        purpose=purpose,
        content_type="image/jpeg",
        settings=settings,
    )
    assert ticket.asset.original_key is not None
    await storage.put_object(
        storage_key=ticket.asset.original_key,
        data=data or jpeg_with_gps(),
        content_type="image/jpeg",
    )
    asset = await media_service.mark_uploaded(session, storage, asset=ticket.asset)
    return await media_service.process_asset(session, storage, asset=asset, settings=settings)


async def make_post(
    session: AsyncSession,
    redis: Redis,
    *,
    author: User,
    body: str = "Has anyone seen a brown dog near the market?",
    category: str = "general",
    media_ids: list[uuid.UUID] | None = None,
) -> Post:
    """A post, created through the real service so its location is the author's home."""
    post, _ = await feed_service.create_post(
        session,
        redis,
        author=author,
        body=body,
        category=category,
        media_ids=media_ids or [],
        settings=get_settings(),
    )
    return post
