"""The adaptive radius engine.

The brief's acceptance criterion for Phase 1: synthetic users around Nyeri prove
the radius shrinks in dense areas, widens in sparse ones, respects its limits, and
does not flicker. Those four claims are the four sections below.
"""

from __future__ import annotations

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.modules.location import cells
from app.modules.location.radius import RadiusEngine, RadiusResult
from app.modules.location.tasks import refresh_cell_activity
from tests.factories import (
    KARATINA,
    NYERI_TOWN,
    make_users_at,
    offset_km,
    set_cell_density,
)


@pytest.fixture
def radius_engine(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> RadiusEngine:
    """Deliberately not named `engine`.

    conftest's `engine` fixture is the SQLAlchemy AsyncEngine that `db_session`
    is built from, so shadowing that name here makes `db_session` depend on a
    fixture that depends on `db_session` — pytest reports it as a recursive
    dependency, several layers from the cause.
    """
    return RadiusEngine(db_session, redis_client, settings)


async def feed_km(radius_engine: RadiusEngine, anchor: int) -> float:
    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)
    return result.radius_km


# ---------------------------------------------------------------------------
# It shrinks where it is dense and widens where it is sparse
# ---------------------------------------------------------------------------
async def test_an_empty_area_sits_at_the_maximum(
    db_session: AsyncSession, radius_engine: RadiusEngine, settings: Settings
) -> None:
    """Launch day. The brief says to expect this, so it is asserted rather than
    treated as an edge case."""
    anchor = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8

    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert result.radius_km == settings.feed_radius_max_km
    assert result.reached_target is False
    assert result.clamped_to_maximum is True


async def test_a_dense_area_shrinks_towards_the_minimum(
    db_session: AsyncSession, radius_engine: RadiusEngine, settings: Settings
) -> None:
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=200, rings=5)

    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert result.reached_target is True
    assert result.radius_km < 3.0
    assert result.rings_expanded <= 2


async def test_a_very_dense_area_is_held_at_the_minimum(
    db_session: AsyncSession, radius_engine: RadiusEngine, settings: Settings
) -> None:
    """The floor exists so a packed estate does not end up with a radius of a few
    hundred metres and a feed of four people's posts."""
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=5_000, rings=0)

    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert result.radius_km == settings.feed_radius_min_km
    assert result.clamped_to_minimum is True


async def test_density_and_radius_move_in_opposite_directions(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> None:
    """The single most important property: monotonicity. More neighbours must
    never mean a wider radius."""
    anchor = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8
    measured: list[float] = []

    for per_cell in (0, 1, 3, 10, 40, 200):
        await db_session.execute(text("DELETE FROM cell_activity"))
        if per_cell:
            await set_cell_density(
                db_session, origin=NYERI_TOWN, active_per_cell=per_cell, rings=12
            )
        await redis_client.flushdb()
        radius_engine = RadiusEngine(db_session, redis_client, settings)
        measured.append(await feed_km(radius_engine, anchor))

    assert measured == sorted(measured, reverse=True), measured
    assert measured[0] > measured[-1]


async def test_a_sparse_area_widens_but_stays_within_the_maximum(
    db_session: AsyncSession, radius_engine: RadiusEngine, settings: Settings
) -> None:
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=2, rings=20)

    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert result.radius_km > 5.0
    assert result.radius_km <= settings.feed_radius_max_km


# ---------------------------------------------------------------------------
# It respects its limits
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("per_cell", [0, 1, 5, 50, 500, 5_000])
async def test_the_feed_radius_is_always_within_its_bounds(
    db_session: AsyncSession,
    redis_client: Redis,
    settings: Settings,
    per_cell: int,
) -> None:
    anchor = await set_cell_density(
        db_session, origin=NYERI_TOWN, active_per_cell=per_cell, rings=15
    )
    radius_engine = RadiusEngine(db_session, redis_client, settings)

    radius = await feed_km(radius_engine, anchor)

    assert settings.feed_radius_min_km <= radius <= settings.feed_radius_max_km


async def test_every_feature_stays_within_its_own_bounds(
    db_session: AsyncSession, radius_engine: RadiusEngine, settings: Settings
) -> None:
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=1, rings=20)

    results = await radius_engine.all_radii(anchor)

    for feature, result in results.items():
        rule = settings.radius_rules[feature]
        assert rule.min_km <= result.radius_km <= rule.max_km, feature


async def test_features_are_multiples_of_the_feed_radius(
    db_session: AsyncSession, radius_engine: RadiusEngine, settings: Settings
) -> None:
    """ "2 x feed radius" means twice the radius the feed actually uses, so the
    multiplier applies to the clamped value."""
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=120, rings=4)

    results = await radius_engine.all_radii(anchor)
    feed = results[RadiusFeature.feed].radius_km

    for feature in (RadiusFeature.groups, RadiusFeature.marketplace, RadiusFeature.events):
        rule = settings.radius_rules[feature]
        expected = min(max(feed * 2.0, rule.min_km), rule.max_km)
        assert results[feature].radius_km == pytest.approx(expected)

    services_rule = settings.radius_rules[RadiusFeature.services]
    assert results[RadiusFeature.services].radius_km == pytest.approx(
        min(max(feed * 3.0, services_rule.min_km), services_rule.max_km)
    )


async def test_services_reaches_furthest_and_the_feed_least(
    db_session: AsyncSession, radius_engine: RadiusEngine
) -> None:
    """You will travel further for a plumber than for gossip."""
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=150, rings=4)

    results = await radius_engine.all_radii(anchor)

    assert results[RadiusFeature.services].radius_km > results[RadiusFeature.feed].radius_km
    assert results[RadiusFeature.groups].radius_km >= results[RadiusFeature.feed].radius_km


# ---------------------------------------------------------------------------
# It does not flicker
# ---------------------------------------------------------------------------
async def test_a_small_density_change_does_not_move_the_radius(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> None:
    """Hysteresis. A radius that moved every refresh would reshuffle the feed
    every fifteen minutes, which a user reads as a bug rather than as adaptation."""
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=60, rings=10)
    radius_engine = RadiusEngine(db_session, redis_client, settings)
    first = await feed_km(radius_engine, anchor)

    await db_session.execute(text("DELETE FROM cell_activity"))
    await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=66, rings=10)
    second = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert second.radius_km == first
    assert second.source == "stabilised"


async def test_a_large_density_change_does_move_the_radius(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> None:
    """Hysteresis must damp noise without freezing genuine change."""
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=60, rings=10)
    radius_engine = RadiusEngine(db_session, redis_client, settings)
    first = await feed_km(radius_engine, anchor)

    await db_session.execute(text("DELETE FROM cell_activity"))
    await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=600, rings=10)
    second = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert second.radius_km < first
    assert second.source == "computed"


async def test_a_repeated_request_is_served_from_the_cache(
    db_session: AsyncSession, redis_client: Redis, radius_engine: RadiusEngine
) -> None:
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=60, rings=8)

    first = await radius_engine.radius_for(RadiusFeature.feed, anchor)
    second = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert second.radius_km == first.radius_km
    assert await redis_client.get(RadiusEngine.cache_key(anchor)) is not None


async def test_invalidating_the_cache_forces_a_recompute(
    db_session: AsyncSession, redis_client: Redis, radius_engine: RadiusEngine
) -> None:
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=60, rings=8)
    await radius_engine.radius_for(RadiusFeature.feed, anchor)

    await radius_engine.invalidate(anchor)

    assert await redis_client.get(RadiusEngine.cache_key(anchor)) is None


async def test_the_engine_works_without_redis(db_session: AsyncSession, settings: Settings) -> None:
    """Losing the cache costs performance and stability, not correctness."""

    class BrokenRedis:
        async def get(self, key: str) -> None:
            raise ConnectionError("down")

        async def set(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("down")

        async def delete(self, *args: object) -> None:
            raise ConnectionError("down")

    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=200, rings=4)
    radius_engine = RadiusEngine(db_session, BrokenRedis(), settings)  # type: ignore[arg-type]

    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert result.reached_target is True
    assert result.radius_km > 0


# ---------------------------------------------------------------------------
# Neighbourhoods are independent
# ---------------------------------------------------------------------------
async def test_density_in_one_town_does_not_shrink_another(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> None:
    """Nyeri filling up must not change what "nearby" means in Karatina, 22 km
    away. This is what makes the radius local rather than global."""
    nyeri_anchor = await set_cell_density(
        db_session, origin=NYERI_TOWN, active_per_cell=400, rings=3
    )
    karatina_anchor = cells.locate(*KARATINA.as_tuple()).cell_res8
    radius_engine = RadiusEngine(db_session, redis_client, settings)

    nyeri = await feed_km(radius_engine, nyeri_anchor)
    karatina = await feed_km(radius_engine, karatina_anchor)

    assert nyeri < 3.0
    assert karatina == settings.feed_radius_max_km


async def test_each_cell_caches_its_own_radius(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> None:
    nyeri_anchor = await set_cell_density(
        db_session, origin=NYERI_TOWN, active_per_cell=400, rings=3
    )
    karatina_anchor = cells.locate(*KARATINA.as_tuple()).cell_res8
    radius_engine = RadiusEngine(db_session, redis_client, settings)

    await feed_km(radius_engine, nyeri_anchor)
    await feed_km(radius_engine, karatina_anchor)

    assert RadiusEngine.cache_key(nyeri_anchor) != RadiusEngine.cache_key(karatina_anchor)
    assert await redis_client.get(RadiusEngine.cache_key(nyeri_anchor)) is not None
    assert await redis_client.get(RadiusEngine.cache_key(karatina_anchor)) is not None


# ---------------------------------------------------------------------------
# Counts come from real users, via the worker
# ---------------------------------------------------------------------------
async def test_real_users_drive_the_radius_through_the_refresh(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> None:
    """End to end: create users, run the density worker, read the radius. If the
    worker and the engine disagreed about resolutions or cells, this is where it
    would show."""
    await make_users_at(db_session, origin=NYERI_TOWN, count=60, radius_km=1.0, seed=3)
    await refresh_cell_activity(db_session, settings=settings)

    anchor = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8
    radius_engine = RadiusEngine(db_session, redis_client, settings)
    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert result.active_users_counted >= 50


async def test_inactive_users_do_not_count_towards_density(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> None:
    """ "Active" means the app was opened recently. Counting dormant accounts
    would shrink the radius for a neighbourhood that is not actually busy, and
    the feed would be empty."""
    await make_users_at(
        db_session, origin=NYERI_TOWN, count=80, radius_km=1.0, seed=5, active=False
    )
    await refresh_cell_activity(db_session, settings=settings)

    anchor = cells.locate(*NYERI_TOWN.as_tuple()).cell_res8
    radius_engine = RadiusEngine(db_session, redis_client, settings)
    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert result.active_users_counted == 0
    assert result.radius_km == settings.feed_radius_max_km


# ---------------------------------------------------------------------------
# Display and user filters
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("radius_km", "expected"),
    [(1.0, 1.0), (1.2, 1.0), (1.3, 1.5), (2.87, 3.0), (14.9, 15.0), (0.1, 0.5)],
)
async def test_display_rounding_is_to_the_half_kilometre(
    radius_engine: RadiusEngine, settings: Settings, radius_km: float, expected: float
) -> None:
    """An exact radius is a read-out of how dense the caller's own street is, so
    what the app shows is rounded."""
    result = RadiusResult(
        feature=RadiusFeature.feed,
        radius_km=radius_km,
        active_users_counted=0,
        rings_expanded=0,
        reached_target=False,
        clamped_to_minimum=False,
        clamped_to_maximum=False,
        source="computed",
    )

    assert result.display_km(settings.radius_display_rounding_km) == expected


async def test_the_feed_has_no_user_distance_filter(radius_engine: RadiusEngine) -> None:
    """Deliberate: "show me everything" is how a local feed stops being local."""
    assert radius_engine.user_filter_cap_km(RadiusFeature.feed) is None


async def test_other_features_expose_their_filter_caps(
    radius_engine: RadiusEngine, settings: Settings
) -> None:
    assert radius_engine.user_filter_cap_km(RadiusFeature.marketplace) == 50.0
    assert radius_engine.user_filter_cap_km(RadiusFeature.groups) == 25.0


async def test_a_user_filter_can_narrow_freely(
    db_session: AsyncSession, radius_engine: RadiusEngine
) -> None:
    """Narrowing is always safe, and someone who wants only their own street
    should be able to say so."""
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=1, rings=20)
    result = await radius_engine.radius_for(RadiusFeature.marketplace, anchor)

    assert radius_engine.effective_radius_km(result, 1.0) == 1.0


async def test_a_user_filter_cannot_widen_past_its_cap(
    db_session: AsyncSession, radius_engine: RadiusEngine
) -> None:
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=1, rings=20)
    result = await radius_engine.radius_for(RadiusFeature.marketplace, anchor)

    assert radius_engine.effective_radius_km(result, 500.0) == 50.0


async def test_the_feed_ignores_a_requested_filter(
    db_session: AsyncSession, radius_engine: RadiusEngine
) -> None:
    anchor = await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=1, rings=20)
    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert radius_engine.effective_radius_km(result, 999.0) == result.radius_km


async def test_radius_metres_is_what_st_dwithin_wants(radius_engine: RadiusEngine) -> None:
    anchor = cells.locate(*offset_km(NYERI_TOWN, north=1.0).as_tuple()).cell_res8

    result = await radius_engine.radius_for(RadiusFeature.feed, anchor)

    assert result.radius_m == round(result.radius_km * 1000)
    assert isinstance(result.radius_m, int)
