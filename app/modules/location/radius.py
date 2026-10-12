"""The adaptive radius engine.

Each user sees a different distance, chosen so that their feed holds roughly the
same number of people wherever they live. In a dense estate the radius shrinks
until the feed is genuinely local; out in the countryside it widens until there is
something to read.

**Computed per cell, not per user.** The unit of computation is the viewer's H3
res-8 cell, so everyone in that cell shares one answer and one cache entry. Per
user it would be the same arithmetic repeated thousands of times for an identical
result, and it would make the radius a function of *who* is asking rather than
*where* — which would leak something about the user.

**How it works.** Rings are walked outward from the viewer's cell, accumulating
the active-user counts the density worker precomputed, until the target is
reached. The ring where that happens is converted to kilometres using real H3
geometry, then clamped to the feature's bounds.

**Only the feed radius is computed.** Every other feature is a multiple of it, as
the brief specifies, so the expensive part happens once and the rest is
arithmetic. That also means features widen and narrow together as an area fills
up, instead of drifting apart.

**Hysteresis.** A radius that moves every fifteen minutes would reshuffle the feed
every fifteen minutes, which reads to a user as a bug rather than as adaptation.
A cached value is only replaced when the new one differs by more than the
configured proportion, so small density wobbles change nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, RadiusRule, Settings
from app.core.redis import CACHE_ERRORS
from app.modules.location import cells
from app.modules.location.models import CellActivity

logger = logging.getLogger(__name__)

CACHE_PREFIX = "radius:feed"

RadiusSource = Literal["computed", "cached", "stabilised"]


@dataclass(frozen=True, slots=True)
class RadiusResult:
    """One feature's radius, with enough context to explain itself."""

    feature: RadiusFeature
    radius_km: float
    active_users_counted: int
    rings_expanded: int
    reached_target: bool
    clamped_to_minimum: bool
    clamped_to_maximum: bool
    source: RadiusSource

    @property
    def radius_m(self) -> int:
        """Metres, for `ST_DWithin`."""
        return round(self.radius_km * 1000)

    def display_km(self, rounding_km: float) -> float:
        """Rounded for display, so the app shows "3 km" rather than "2.87 km".

        Not only cosmetic: an exact radius is a direct read-out of how dense the
        caller's own immediate area is, which is information about their
        neighbours rather than about them.
        """
        rounded = round(self.radius_km / rounding_km) * rounding_km
        # Never round down to nothing; the smallest meaningful answer is one step.
        return max(rounding_km, round(rounded, 3))


@dataclass(frozen=True, slots=True)
class _FeedRadius:
    """The computed feed radius before per-feature derivation."""

    radius_km: float
    """The feed's actual radius, already clamped to the feed's own bounds. Other
    features multiply this rather than the raw value, because the brief's
    "2 x feed radius" means twice the radius the feed really uses."""

    raw_ring_km: float
    """The unclamped distance the ring walk reached. Kept only so the result can
    report honestly whether a clamp was applied; a pre-clamped value would make
    every dense area look as though it had landed naturally on the minimum."""

    active_users_counted: int
    rings_expanded: int
    reached_target: bool
    source: RadiusSource


class RadiusEngine:
    """Computes and caches radii for one anchor cell at a time."""

    def __init__(self, session: AsyncSession, redis: Redis, settings: Settings) -> None:
        self._session = session
        self._redis = redis
        self._settings = settings

    # -- Public API ----------------------------------------------------------
    async def radius_for(self, feature: RadiusFeature, anchor_cell_res8: int) -> RadiusResult:
        feed = await self._feed_radius(anchor_cell_res8)
        return self._derive(feature, feed)

    async def all_radii(self, anchor_cell_res8: int) -> dict[RadiusFeature, RadiusResult]:
        """Every feature's radius from one density computation."""
        feed = await self._feed_radius(anchor_cell_res8)
        return {feature: self._derive(feature, feed) for feature in RadiusFeature}

    # -- Derivation ----------------------------------------------------------
    def _derive(self, feature: RadiusFeature, feed: _FeedRadius) -> RadiusResult:
        rule = self._settings.radius_rules[feature]
        # The feed compares against the unclamped ring distance; every other
        # feature is a multiple of the feed's clamped radius.
        raw_km = (
            feed.raw_ring_km
            if rule.feed_multiplier is None
            else feed.radius_km * rule.feed_multiplier
        )
        clamped_km = min(max(raw_km, rule.min_km), rule.max_km)
        return RadiusResult(
            feature=feature,
            radius_km=clamped_km,
            active_users_counted=feed.active_users_counted,
            rings_expanded=feed.rings_expanded,
            reached_target=feed.reached_target,
            clamped_to_minimum=clamped_km > raw_km,
            clamped_to_maximum=clamped_km < raw_km,
            source=feed.source,
        )

    def user_filter_cap_km(self, feature: RadiusFeature) -> float | None:
        """How far a user may widen this feature with their own distance filter."""
        return self._settings.radius_rules[feature].user_filter_max_km

    def effective_radius_km(self, result: RadiusResult, requested_km: float | None) -> float:
        """Apply a user's distance filter on top of the adaptive radius.

        A filter can only widen up to the feature's cap, and can narrow freely —
        narrowing is always safe, and someone who wants to see only their own
        street should be able to.
        """
        if requested_km is None:
            return result.radius_km
        cap = self.user_filter_cap_km(result.feature)
        if cap is None:
            # The feed has no filter on purpose: "show me everything" is how a
            # local feed stops being local.
            return result.radius_km
        return min(max(requested_km, 0.1), cap)

    # -- Computation ---------------------------------------------------------
    async def _feed_radius(self, anchor_cell_res8: int) -> _FeedRadius:
        cached_km = await self._read_cache(anchor_cell_res8)
        computed = await self._compute_feed_radius(anchor_cell_res8)

        if cached_km is None:
            await self._write_cache(anchor_cell_res8, computed.radius_km)
            return computed

        if self._within_hysteresis(cached_km, computed.radius_km):
            # Keep the cached value and refresh its TTL, so a stable area stops
            # recomputing rather than recomputing and discarding the answer.
            await self._write_cache(anchor_cell_res8, cached_km)
            return _FeedRadius(
                radius_km=cached_km,
                raw_ring_km=computed.raw_ring_km,
                active_users_counted=computed.active_users_counted,
                rings_expanded=computed.rings_expanded,
                reached_target=computed.reached_target,
                source="stabilised",
            )

        await self._write_cache(anchor_cell_res8, computed.radius_km)
        logger.info(
            "radius_moved",
            extra={
                "from_km": round(cached_km, 2),
                "to_km": round(computed.radius_km, 2),
                "rings": computed.rings_expanded,
            },
        )
        return computed

    def _within_hysteresis(self, cached_km: float, computed_km: float) -> bool:
        if cached_km <= 0:
            return False
        change = abs(computed_km - cached_km) / cached_km
        return change <= self._settings.radius_hysteresis_ratio

    async def _compute_feed_radius(self, anchor_cell_res8: int) -> _FeedRadius:
        rule = self._settings.radius_rules[RadiusFeature.feed]
        target = rule.target_active_users or 1

        # Bounded by the feed's own maximum rather than by the global ring cap:
        # a radius can never exceed max_km, so counting people beyond it would be
        # work whose result is thrown away. At res 8 this is roughly 17 rings for
        # a 15 km maximum, against 40 for the cap.
        ring_limit = self._ring_limit_for_km(anchor_cell_res8, rule.max_km)
        counts = await self._active_counts_within(anchor_cell_res8, ring_limit)

        running_total = 0
        for k in range(ring_limit + 1):
            running_total += counts.get(k, 0)
            if running_total >= target:
                raw_km = cells.ring_distance_km(anchor_cell_res8, k)
                return _FeedRadius(
                    radius_km=min(max(raw_km, rule.min_km), rule.max_km),
                    raw_ring_km=raw_km,
                    active_users_counted=running_total,
                    rings_expanded=k,
                    reached_target=True,
                    source="computed",
                )

        # Not enough people anywhere in range. This is the expected state at
        # launch, not an error: the brief says most users will sit at the maximum.
        return _FeedRadius(
            radius_km=rule.max_km,
            raw_ring_km=cells.ring_distance_km(anchor_cell_res8, ring_limit),
            active_users_counted=running_total,
            rings_expanded=ring_limit,
            reached_target=False,
            source="computed",
        )

    def _ring_limit_for_km(self, anchor_cell_res8: int, max_km: float) -> int:
        """The fewest rings that reach at least `max_km`, within the safety cap."""
        cap = self._settings.radius_max_ring_k
        for k in range(cap + 1):
            if cells.ring_distance_km(anchor_cell_res8, k) >= max_km:
                return k
        return cap

    async def _active_counts_within(self, anchor_cell_res8: int, ring_limit: int) -> dict[int, int]:
        """Active-user counts grouped by ring distance from the anchor.

        One query for the whole disk, then grouped in Python. The alternative —
        a query per ring — is up to seventeen round trips for the same rows, and
        at launch density most of those rows do not exist at all, so the single
        query returns almost nothing.
        """
        disk = cells.disk(anchor_cell_res8, ring_limit)
        statement = select(CellActivity.cell, CellActivity.active_user_count).where(
            CellActivity.resolution == self._settings.h3_radius_resolution,
            CellActivity.cell.in_(disk),
            CellActivity.active_user_count > 0,
        )
        rows = (await self._session.execute(statement)).all()

        by_ring: dict[int, int] = {}
        for cell, count in rows:
            k = cells.grid_distance(anchor_cell_res8, int(cell))
            by_ring[k] = by_ring.get(k, 0) + int(count)
        return by_ring

    # -- Cache ---------------------------------------------------------------
    @staticmethod
    def cache_key(anchor_cell_res8: int) -> str:
        return f"{CACHE_PREFIX}:{anchor_cell_res8}"

    async def _read_cache(self, anchor_cell_res8: int) -> float | None:
        """A cache miss is not an error: the radius is simply recomputed.

        Only the feed radius is cached. Every other feature is a fixed multiple of
        it, so stabilising the feed stabilises all of them, and caching five
        derived values would be five round trips to avoid one multiplication.
        """
        try:
            raw = await self._redis.get(self.cache_key(anchor_cell_res8))
        except CACHE_ERRORS:
            logger.warning("radius_cache_unavailable", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def _write_cache(self, anchor_cell_res8: int, radius_km: float) -> None:
        try:
            await self._redis.set(
                self.cache_key(anchor_cell_res8),
                f"{radius_km:.4f}",
                ex=self._settings.radius_cache_ttl_seconds,
            )
        except CACHE_ERRORS:
            # Losing the cache costs performance and stability, not correctness.
            logger.warning("radius_cache_write_failed", exc_info=True)

    async def invalidate(self, anchor_cell_res8: int) -> None:
        """Drop a cached radius. Used by tests and by density backfills."""
        try:
            await self._redis.delete(self.cache_key(anchor_cell_res8))
        except CACHE_ERRORS:
            logger.warning("radius_cache_invalidate_failed", exc_info=True)


def rule_for(settings: Settings, feature: RadiusFeature) -> RadiusRule:
    return settings.radius_rules[feature]
