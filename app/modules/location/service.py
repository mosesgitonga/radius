"""Location service: setting home, labelling areas, and explore mode.

Three jobs, all of which exist to let the product answer "near you" without ever
knowing where anyone is.

**Setting home** converts coordinates to cells and throws the coordinates away.
It also enforces the change cooldown, which is the real anti-abuse control: it
stops someone hopping between neighbourhoods to spam each in turn, while leaving
a student who moves between campus and home over the holidays free to switch.

**Labelling** turns a cell into a human-readable area name, but only at a
granularity that satisfies k-anonymity. If fewer than K registered users share
the cell, the label snaps up to a coarser one, and eventually to a generic town
name. The failure mode is deliberately towards privacy: when the counts are not
known — a fresh deployment where the density worker has not yet run — everyone
gets the generic label rather than a precise one.

**Explore mode** keeps a temporary anchor in Redis for a couple of hours and never
writes it to PostgreSQL. The location of someone who is out and about is the most
sensitive thing in the system, and the cheapest way to protect it is for it not to
exist anywhere durable.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import h3
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError
from app.core.redis import CACHE_ERRORS
from app.modules.location import cells
from app.modules.location.cells import CellLocation, InvalidCoordinatesError
from app.modules.location.models import Area, CellActivity
from app.modules.moderation.models import AuditAction
from app.modules.moderation.service import record_audit_event
from app.modules.users.models import HOME_PRESENCE_WINDOW_DAYS, User

logger = logging.getLogger(__name__)

EXPLORE_KEY_PREFIX = "explore"
AREA_LABEL_KEY_PREFIX = "arealabel"
AREA_LABEL_CACHE_TTL_SECONDS = 3600

GENERIC_LABEL = "nearby"

AnchorMode = Literal["home", "explore"]


@dataclass(frozen=True, slots=True)
class AreaLabel:
    """A location as it is shown to other people.

    Carries no cell, no coordinate, and no resolution that could be turned back
    into a place. `precision` is a coarse hint for the client ("neighbourhood" or
    "town"), not a number anybody can compute with.
    """

    label: str
    precision: Literal["neighbourhood", "area", "town", "generic"]
    area_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class HomeLocationOutcome:
    location: CellLocation
    area_label: AreaLabel
    is_first_time: bool
    spoof_suspected: bool


@dataclass(frozen=True, slots=True)
class ExploreLocation:
    cell_res9: int
    cell_res8: int
    expires_at: datetime


# ---------------------------------------------------------------------------
# Home location
# ---------------------------------------------------------------------------
def _to_cell_location(latitude: float, longitude: float, settings: Settings) -> CellLocation:
    try:
        return cells.locate(latitude, longitude, storage_resolution=settings.h3_storage_resolution)
    except InvalidCoordinatesError as exc:
        # The message deliberately does not echo the value back: an error payload
        # is logged, and these are coordinates.
        raise BadRequestError(str(exc)) from exc


async def _implied_speed_kmh(
    session: AsyncSession, user: User, new_location: CellLocation
) -> float | None:
    """Speed implied by the move from the user's current home, if there is one.

    Measured centroid to centroid, so it inherits the same coarseness as
    everything else and cannot be used to infer a precise previous position.
    """
    if user.home_cell_res9 is None or user.home_set_at is None:
        return None

    elapsed = datetime.now(UTC) - user.home_set_at
    elapsed_hours = elapsed.total_seconds() / 3600
    if elapsed_hours <= 0:
        return None

    previous_latitude, previous_longitude = cells.cell_centroid(user.home_cell_res9)
    distance_km = float(
        h3.great_circle_distance(
            (previous_latitude, previous_longitude),
            (new_location.centroid_latitude, new_location.centroid_longitude),
            unit="km",
        )
    )
    return distance_km / elapsed_hours


async def set_home_location(
    session: AsyncSession,
    redis: Redis,
    *,
    user: User,
    latitude: float,
    longitude: float,
    settings: Settings,
) -> HomeLocationOutcome:
    """Set or move the user's home area.

    Raises `ConflictError` if the cooldown has not elapsed. An implausible jump is
    recorded for review but **not** rejected: the brief asks for it to be flagged,
    and refusing would lock out anyone who genuinely flew between towns while
    doing nothing to stop a patient attacker.
    """
    location = _to_cell_location(latitude, longitude, settings)
    is_first_time = user.home_cell_res9 is None
    now = datetime.now(UTC)

    unchanged = user.home_cell_res9 == location.cell_res9
    if not is_first_time and not unchanged:
        _enforce_change_cooldown(user, settings, now)

    speed_kmh = await _implied_speed_kmh(session, user, location)
    spoof_suspected = speed_kmh is not None and speed_kmh > settings.location_spoof_max_speed_kmh
    if spoof_suspected:
        logger.warning(
            "location_spoof_suspected",
            extra={"user_id": str(user.id), "implied_speed_kmh": round(speed_kmh or 0, 1)},
        )
        await record_audit_event(
            session,
            action=AuditAction.location_spoof_suspected,
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            # The speed, never the positions it was derived from.
            context={"implied_speed_kmh": round(speed_kmh or 0, 1)},
        )

    user.home_cell_res9 = location.cell_res9
    user.home_cell_res8 = location.cell_res8
    user.home_cell_res7 = location.cell_res7
    user.home_cell_res6 = location.cell_res6
    user.home_centroid = location.centroid_wkt
    user.home_area_id = await _resolve_area_id(
        session, location.centroid_latitude, location.centroid_longitude
    )
    user.home_set_at = now
    if not is_first_time and not unchanged:
        user.home_changed_at = now
        # The badge measures consistency with a home area that has just changed,
        # so the window starts again. Vouching is the fast path back (Phase 8).
        user.home_presence_mask = 0
        user.home_presence_updated_on = None

    await record_audit_event(
        session,
        action=(
            AuditAction.home_location_set if is_first_time else AuditAction.home_location_changed
        ),
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        context={"first_time": is_first_time, "changed": not unchanged},
    )

    record_home_presence(
        user,
        reported_cell_res9=location.cell_res9,
        today=now.date(),
        settings=settings,
    )

    label = await area_label_for_cells(session, redis, user=user, settings=settings)
    return HomeLocationOutcome(
        location=location,
        area_label=label,
        is_first_time=is_first_time,
        spoof_suspected=spoof_suspected,
    )


def _enforce_change_cooldown(user: User, settings: Settings, now: datetime) -> None:
    if settings.home_change_cooldown_days <= 0 or user.home_changed_at is None:
        return
    next_allowed = user.home_changed_at + timedelta(days=settings.home_change_cooldown_days)
    if now < next_allowed:
        raise ConflictError(
            "Your home area was changed recently and cannot be changed again yet",
            details={
                "next_change_allowed_at": next_allowed.isoformat(),
                "cooldown_days": settings.home_change_cooldown_days,
            },
        )


async def _resolve_area_id(
    session: AsyncSession, latitude: float, longitude: float
) -> uuid.UUID | None:
    """The smallest active seeded area covering a point, if any.

    Smallest, so an estate wins over the ward and town that also contain it. A
    user outside every seeded area is normal — it is what happens in a town
    nobody has seeded yet — so this returns None rather than raising.
    """
    point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
    statement = (
        select(Area.id)
        .where(Area.is_active.is_(True), func.ST_Covers(Area.boundary, point))
        .order_by(func.ST_Area(Area.boundary).asc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Area labels and k-anonymity
# ---------------------------------------------------------------------------
_PRECISION_BY_OFFSET: dict[int, Literal["neighbourhood", "area", "town"]] = {
    0: "neighbourhood",
    1: "area",
    2: "town",
}


async def area_label_for_cells(
    session: AsyncSession,
    redis: Redis,
    *,
    user: User,
    settings: Settings,
) -> AreaLabel:
    """The label shown for this user's home area."""
    if user.home_cell_res9 is None:
        return AreaLabel(label=GENERIC_LABEL, precision="generic")
    return await area_label_for_cell(
        session,
        redis,
        cell_res9=user.home_cell_res9,
        settings=settings,
    )


async def area_label_for_cell(
    session: AsyncSession,
    redis: Redis,
    *,
    cell_res9: int,
    settings: Settings,
) -> AreaLabel:
    """Resolve a res-9 cell to a k-anonymous area label.

    Walks the coarse resolutions from finest to coarsest and stops at the first
    one shared by at least K registered users. If none qualifies — a genuinely
    remote user, or a deployment whose density worker has not run — the result is
    the generic label, which is the safe direction to fail in.
    """
    cached = await _read_label_cache(redis, cell_res9)
    if cached is not None:
        return cached

    label = await _compute_area_label(session, cell_res9=cell_res9, settings=settings)
    await _write_label_cache(redis, cell_res9, label)
    return label


async def _compute_area_label(
    session: AsyncSession, *, cell_res9: int, settings: Settings
) -> AreaLabel:
    minimum = settings.k_anonymity_min_users

    for offset, resolution in enumerate(settings.h3_coarse_resolutions):
        coarse_cell = cells.parent_cell(cell_res9, resolution)
        registered = await _registered_in_cell(session, resolution, coarse_cell)
        if registered < minimum:
            continue

        latitude, longitude = cells.cell_centroid(coarse_cell)
        area = await _area_covering(session, latitude, longitude)
        if area is not None:
            return AreaLabel(
                label=area.name,
                precision=_PRECISION_BY_OFFSET.get(offset, "town"),
                area_id=area.id,
            )
        # The cell satisfies k-anonymity but falls outside every seeded polygon.
        # Name the town if one is known nearby rather than giving up entirely.
        nearby_town = await _nearest_town_name(session, latitude, longitude)
        if nearby_town:
            return AreaLabel(label=f"near {nearby_town}", precision="town")

    return AreaLabel(label=GENERIC_LABEL, precision="generic")


async def _registered_in_cell(session: AsyncSession, resolution: int, cell: int) -> int:
    """How many registered users share a coarse cell.

    Read from the precomputed `cell_activity` table rather than counted live: this
    runs on every profile render, and a `COUNT(*)` over users each time would make
    a cheap read expensive. A missing row counts as zero, which pushes the label
    coarser — the safe direction.
    """
    statement = select(CellActivity.registered_user_count).where(
        CellActivity.resolution == resolution, CellActivity.cell == cell
    )
    return (await session.execute(statement)).scalar_one_or_none() or 0


async def _area_covering(session: AsyncSession, latitude: float, longitude: float) -> Area | None:
    point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
    statement = (
        select(Area)
        .where(Area.is_active.is_(True), func.ST_Covers(Area.boundary, point))
        .order_by(func.ST_Area(Area.boundary).asc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def _nearest_town_name(
    session: AsyncSession, latitude: float, longitude: float
) -> str | None:
    """The closest seeded town, for the "near Nyeri town" fallback.

    Capped at 50 km so a user in another county is not told they are near a town
    they have never heard of; beyond that the generic label is more honest.
    """
    point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
    statement = (
        select(Area.name)
        .where(
            Area.is_active.is_(True),
            Area.kind == "town",
            func.ST_DWithin(Area.boundary, point, 50_000),
        )
        .order_by(func.ST_Distance(Area.boundary, point).asc())
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


def _label_cache_key(cell_res9: int) -> str:
    return f"{AREA_LABEL_KEY_PREFIX}:{cell_res9}"


async def _read_label_cache(redis: Redis, cell_res9: int) -> AreaLabel | None:
    try:
        raw = await redis.get(_label_cache_key(cell_res9))
    except CACHE_ERRORS:
        return None
    if not raw:
        return None
    try:
        payload: dict[str, Any] = json.loads(raw)
        area_id = payload.get("area_id")
        return AreaLabel(
            label=payload["label"],
            precision=payload["precision"],
            area_id=uuid.UUID(area_id) if area_id else None,
        )
    except (ValueError, KeyError, TypeError):
        return None


async def _write_label_cache(redis: Redis, cell_res9: int, label: AreaLabel) -> None:
    payload = {
        "label": label.label,
        "precision": label.precision,
        "area_id": str(label.area_id) if label.area_id else None,
    }
    try:
        await redis.set(
            _label_cache_key(cell_res9),
            json.dumps(payload),
            ex=AREA_LABEL_CACHE_TTL_SECONDS,
        )
    except CACHE_ERRORS:
        logger.warning("area_label_cache_write_failed")


# ---------------------------------------------------------------------------
# Explore mode
# ---------------------------------------------------------------------------
def _explore_key(user_id: uuid.UUID) -> str:
    return f"{EXPLORE_KEY_PREFIX}:{user_id}"


async def start_explore(
    redis: Redis,
    *,
    user_id: uuid.UUID,
    latitude: float,
    longitude: float,
    settings: Settings,
) -> ExploreLocation:
    """Begin exploring somewhere else.

    Redis only, with a TTL, and never written to PostgreSQL — so there is nothing
    to export, nothing to purge, and nothing to breach. Authored content still
    carries the user's home area, so exploring cannot be used to post into a
    neighbourhood the user does not live in.
    """
    location = _to_cell_location(latitude, longitude, settings)
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.explore_ttl_seconds)
    payload = json.dumps(
        {
            "cell_res9": location.cell_res9,
            "cell_res8": location.cell_res8,
            "expires_at": expires_at.isoformat(),
        }
    )
    await redis.set(_explore_key(user_id), payload, ex=settings.explore_ttl_seconds)
    return ExploreLocation(
        cell_res9=location.cell_res9,
        cell_res8=location.cell_res8,
        expires_at=expires_at,
    )


async def get_explore(redis: Redis, *, user_id: uuid.UUID) -> ExploreLocation | None:
    try:
        raw = await redis.get(_explore_key(user_id))
    except CACHE_ERRORS:
        # Redis is the only store for this, so an outage simply ends explore mode
        # and the user falls back to their home feed. Degraded, not broken.
        logger.warning("explore_lookup_failed")
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return ExploreLocation(
            cell_res9=int(payload["cell_res9"]),
            cell_res8=int(payload["cell_res8"]),
            expires_at=datetime.fromisoformat(payload["expires_at"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


async def stop_explore(redis: Redis, *, user_id: uuid.UUID) -> None:
    try:
        await redis.delete(_explore_key(user_id))
    except CACHE_ERRORS:
        logger.warning("explore_clear_failed")


async def resolve_anchor_cell(redis: Redis, *, user: User, mode: AnchorMode = "home") -> int | None:
    """The res-8 cell every radius and proximity query is measured from.

    One function so that "which location am I querying from?" has exactly one
    answer across the whole product. Explore mode falls back to home when the
    session has expired, which is what a user would expect after leaving the app
    for an afternoon.
    """
    if mode == "explore":
        explored = await get_explore(redis, user_id=user.id)
        if explored is not None:
            return explored.cell_res8
    return user.home_cell_res8


# ---------------------------------------------------------------------------
# Home presence, for the verified-neighbour badge
# ---------------------------------------------------------------------------
def roll_presence_mask(
    mask: int, last_updated: date | None, today: date, *, window_days: int
) -> int:
    """Age a presence mask forward to today.

    Bit 0 is today and higher bits are older days, so advancing a day is a left
    shift. A gap of several days shifts by that many, which is the whole reason
    `home_presence_updated_on` exists: shifting by one per update would record a
    user who was away for a week as though they had been present all along.
    """
    if last_updated is None:
        return 0
    elapsed = (today - last_updated).days
    if elapsed <= 0:
        return mask
    if elapsed >= window_days:
        # Away longer than the window: nothing from before it survives.
        return 0
    return (mask << elapsed) & ((1 << window_days) - 1)


def record_home_presence(
    user: User, *, reported_cell_res9: int | None, today: date, settings: Settings
) -> None:
    """Record whether the user was in their home area today.

    This is the *only* thing the badge ever learns about where somebody was: one
    bit per day, never a position. See decision 1.1.

    Compared at resolution 7 rather than 9, because "in my home area" means the
    neighbourhood, not the exact cell — a user at the shops two streets away is
    still at home in every sense that matters.

    `reported_cell_res9` of None means the day is **excluded**, neither a yes nor
    a no. That covers explore mode: penalising someone for deliberately browsing
    elsewhere would make the badge measure "did not travel" rather than
    "lives here".
    """
    window = HOME_PRESENCE_WINDOW_DAYS
    user.home_presence_mask = roll_presence_mask(
        user.home_presence_mask, user.home_presence_updated_on, today, window_days=window
    )
    user.home_presence_updated_on = today

    if reported_cell_res9 is None or user.home_cell_res7 is None:
        return

    reported_res7 = cells.parent_cell(reported_cell_res9, settings.h3_storage_resolution - 2)
    if reported_res7 == user.home_cell_res7:
        user.home_presence_mask |= 1


def presence_days_recorded(mask: int) -> int:
    """How many of the recorded days were spent in the home area."""
    return bin(mask).count("1")
