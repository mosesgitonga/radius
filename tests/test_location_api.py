"""Location endpoints, and the privacy properties that matter most.

The privacy section is the point of this file. Section 6.4 of the brief is
non-negotiable, so each rule in it gets a test that would fail if the rule were
quietly relaxed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.modules.auth import service as auth_service
from app.modules.location import cells
from app.modules.location import service as location_service
from app.modules.location.models import CellActivity
from app.modules.moderation.models import AuditAction, AuditEvent
from app.modules.users.models import User
from tests.factories import (
    DEKUT_CAMPUS,
    KARATINA,
    NAIROBI,
    NYERI_TOWN,
    make_area,
    make_user,
    offset_km,
    set_cell_density,
    set_registered_density,
)

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


# ---------------------------------------------------------------------------
# Setting a home location
# ---------------------------------------------------------------------------
async def test_setting_a_location_for_the_first_time(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    fresh = await make_user(db_session)
    pair = await auth_service.issue_tokens(db_session, user=fresh, settings=get_settings())
    await db_session.flush()
    auth_client.headers["Authorization"] = f"Bearer {pair.access_token}"

    response = await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["is_first_time"] is True
    assert body["area_label"]["label"]


async def test_the_response_contains_no_coordinates(
    auth_client: httpx.AsyncClient,
) -> None:
    """The request carried them; the response must not."""
    response = await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    rendered = response.text
    assert str(NYERI_TOWN.latitude) not in rendered
    assert str(NYERI_TOWN.longitude) not in rendered
    assert "latitude" not in rendered
    assert "centroid" not in rendered


async def test_only_the_cell_and_centroid_are_stored(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, user: User
) -> None:
    """The chokepoint the privacy model depends on: what is stored is the cell's
    centre, so the position that was sent is not recoverable."""
    await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())
    await db_session.refresh(user)

    expected = cells.locate(*NYERI_TOWN.as_tuple())
    assert user.home_cell_res9 == expected.cell_res9
    assert user.home_cell_res8 == expected.cell_res8

    stored = await db_session.scalar(
        select(func.ST_AsText(User.home_centroid)).where(User.id == user.id)
    )
    # The stored point is the cell centre, not what the client sent.
    assert stored is not None
    assert str(NYERI_TOWN.latitude) not in stored
    assert str(NYERI_TOWN.longitude) not in stored


async def test_the_area_is_resolved_from_seeded_polygons(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    user: User,
) -> None:
    await make_area(db_session, name="Kamakwa", centre=NYERI_TOWN, width_km=3.0)
    await set_registered_density(db_session, at=NYERI_TOWN, registered=25)
    await redis_client.flushdb()

    response = await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    assert response.json()["area_label"]["label"] == "Kamakwa"
    await db_session.refresh(user)
    assert user.home_area_id is not None


async def test_a_location_outside_every_area_still_works(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, user: User
) -> None:
    """Normal before a town is seeded, so it must not be an error."""
    response = await auth_client.put("/v1/me/location", json=NANYUKI_PAYLOAD)

    assert response.status_code == 200
    await db_session.refresh(user)
    assert user.home_area_id is None


NANYUKI_PAYLOAD = {"latitude": 0.0167, "longitude": 37.0725}


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 91.0, "longitude": 36.0},
        {"latitude": -0.42, "longitude": 181.0},
        {"latitude": "north", "longitude": 36.0},
        {"longitude": 36.0},
        {},
    ],
)
async def test_bad_coordinates_are_refused(
    auth_client: httpx.AsyncClient, payload: dict[str, object]
) -> None:
    response = await auth_client.put("/v1/me/location", json=payload)

    assert response.status_code in (400, 422)


async def test_a_rejected_location_does_not_echo_the_value(
    auth_client: httpx.AsyncClient,
) -> None:
    """Pydantic includes the offending input by default, which here would put
    coordinates into an error payload and therefore into the logs."""
    response = await auth_client.put(
        "/v1/me/location", json={"latitude": -0.4201341, "longitude": "west"}
    )

    assert "-0.4201341" not in response.text


async def test_setting_the_same_location_again_is_not_a_change(
    auth_client: httpx.AsyncClient,
) -> None:
    """Otherwise a client re-reporting the same place would start a cooldown."""
    await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    second = await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    assert second.status_code == 200
    assert second.json()["next_change_allowed_at"] is None


async def test_moving_home_is_allowed_once_then_cooled_down(
    auth_client: httpx.AsyncClient, settings: Settings
) -> None:
    """Stops someone hopping between neighbourhoods to spam each in turn."""
    await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    moved = await auth_client.put("/v1/me/location", json=KARATINA.as_payload())
    again = await auth_client.put("/v1/me/location", json=NAIROBI.as_payload())

    assert moved.status_code == 200
    assert moved.json()["next_change_allowed_at"] is not None
    assert again.status_code == 409
    assert again.json()["error"]["details"]["cooldown_days"] == (settings.home_change_cooldown_days)


async def test_the_cooldown_expires(
    db_session: AsyncSession,
    redis_client: Redis,
    settings: Settings,
    user: User,
) -> None:
    """A DeKUT student moving between campus and home over the holidays has to be
    able to switch."""
    user.home_changed_at = datetime.now(UTC) - timedelta(
        days=settings.home_change_cooldown_days + 1
    )
    await db_session.flush()

    outcome = await location_service.set_home_location(
        db_session,
        redis_client,
        user=user,
        latitude=DEKUT_CAMPUS.latitude,
        longitude=DEKUT_CAMPUS.longitude,
        settings=settings,
    )

    assert outcome.location.cell_res9 == cells.locate(*DEKUT_CAMPUS.as_tuple()).cell_res9


async def test_setting_a_location_is_audited(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.action.in_(
                        [AuditAction.home_location_set, AuditAction.home_location_changed]
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    assert events


async def test_the_audit_entry_holds_no_location(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The audit log is kept for two years and read by humans during incidents,
    which makes it the worst place for the data the rest of the system avoids
    storing."""
    await auth_client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    events = (await db_session.execute(select(AuditEvent))).scalars().all()

    for event in events:
        rendered = str(event.context)
        assert "latitude" not in rendered
        assert str(NYERI_TOWN.latitude) not in rendered


async def test_an_implausible_jump_is_flagged_but_not_refused(
    db_session: AsyncSession,
    redis_client: Redis,
    settings: Settings,
) -> None:
    """Flagged, as the brief asks. Refusing would lock out anybody who genuinely
    flew between towns while doing nothing to stop a patient attacker."""
    traveller = await make_user(db_session, at=NYERI_TOWN)
    traveller.home_set_at = datetime.now(UTC) - timedelta(seconds=1)
    traveller.home_changed_at = None
    await db_session.flush()

    outcome = await location_service.set_home_location(
        db_session,
        redis_client,
        user=traveller,
        latitude=NAIROBI.latitude,
        longitude=NAIROBI.longitude,
        settings=settings,
    )

    assert outcome.spoof_suspected is True

    # Explicit flush: the session runs with autoflush disabled, so the audit row
    # the service added is not visible to a query until it is flushed.
    await db_session.flush()
    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.location_spoof_suspected)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert "implied_speed_kmh" in events[0].context


async def test_spoof_suspicion_is_not_reported_to_the_client(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, user: User
) -> None:
    """Telling a caller their jump looked implausible teaches them what to avoid."""
    user.home_set_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    response = await auth_client.put("/v1/me/location", json=NAIROBI.as_payload())

    assert "spoof" not in response.text.lower()


async def test_setting_a_location_requires_authentication(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put("/v1/me/location", json=NYERI_TOWN.as_payload())

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Area labels and k-anonymity
# ---------------------------------------------------------------------------
async def test_a_sparse_cell_gets_a_generic_label(
    db_session: AsyncSession, redis_client: Redis, settings: Settings, user: User
) -> None:
    """Below K registered users the label must not name the place, because a name
    plus a small population identifies a person."""
    await make_area(db_session, name="Tiny Estate", centre=NYERI_TOWN, width_km=1.0)

    label = await location_service.area_label_for_cells(
        db_session, redis_client, user=user, settings=settings
    )

    assert label.label == "nearby"
    assert label.precision == "generic"


async def test_enough_neighbours_unlocks_the_precise_label(
    db_session: AsyncSession, redis_client: Redis, settings: Settings, user: User
) -> None:
    await make_area(db_session, name="Kamakwa", centre=NYERI_TOWN, width_km=3.0)
    await set_registered_density(
        db_session, at=NYERI_TOWN, registered=settings.k_anonymity_min_users
    )
    await redis_client.flushdb()

    label = await location_service.area_label_for_cells(
        db_session, redis_client, user=user, settings=settings
    )

    assert label.label == "Kamakwa"
    assert label.precision == "neighbourhood"


async def test_one_fewer_than_k_is_still_too_few(
    db_session: AsyncSession, redis_client: Redis, settings: Settings, user: User
) -> None:
    """The boundary case, since an off-by-one here is a privacy failure."""
    await make_area(db_session, name="Kamakwa", centre=NYERI_TOWN, width_km=3.0)
    await set_registered_density(
        db_session, at=NYERI_TOWN, registered=settings.k_anonymity_min_users - 1
    )
    await redis_client.flushdb()

    label = await location_service.area_label_for_cells(
        db_session, redis_client, user=user, settings=settings
    )

    assert label.precision == "generic"


async def test_the_label_snaps_up_to_a_coarser_cell(
    db_session: AsyncSession, redis_client: Redis, settings: Settings, user: User
) -> None:
    """When the fine cell is too sparse but a coarser one is not, the label
    widens rather than disappearing."""
    await make_area(db_session, name="Nyeri Town", centre=NYERI_TOWN, width_km=8.0)
    location = cells.locate(*NYERI_TOWN.as_tuple())
    db_session.add(
        CellActivity(
            resolution=8,
            cell=location.cell_res8,
            active_user_count=0,
            registered_user_count=2,
        )
    )
    db_session.add(
        CellActivity(
            resolution=7,
            cell=location.cell_res7,
            active_user_count=0,
            registered_user_count=40,
        )
    )
    await db_session.flush()
    await redis_client.flushdb()

    label = await location_service.area_label_for_cells(
        db_session, redis_client, user=user, settings=settings
    )

    assert label.label == "Nyeri Town"
    assert label.precision == "area"


async def test_a_user_with_no_location_has_no_label(
    db_session: AsyncSession, redis_client: Redis, settings: Settings
) -> None:
    nowhere = await make_user(db_session)

    label = await location_service.area_label_for_cells(
        db_session, redis_client, user=nowhere, settings=settings
    )

    assert label.precision == "generic"


async def test_the_label_never_carries_a_cell_id(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    user: User,
) -> None:
    await make_area(db_session, name="Kamakwa", centre=NYERI_TOWN, width_km=3.0)
    await set_registered_density(db_session, at=NYERI_TOWN, registered=30)
    await redis_client.flushdb()

    body = (await auth_client.get("/v1/me")).json()

    assert set(body["area_label"]) == {"label", "precision"}
    assert str(user.home_cell_res9) not in str(body)


# ---------------------------------------------------------------------------
# Explore mode
# ---------------------------------------------------------------------------
async def test_explore_returns_a_label_and_an_expiry(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.post("/v1/me/explore", json=KARATINA.as_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["area_label"]["label"]
    assert body["expires_at"]
    assert "home area" in body["note"]


async def test_explore_never_touches_the_database(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, user: User
) -> None:
    """The point of decision 1.1: an explored location is never at rest, so there
    is nothing to export, nothing to purge, and nothing to breach."""
    before = user.home_cell_res9

    await auth_client.post("/v1/me/explore", json=KARATINA.as_payload())
    await db_session.refresh(user)

    assert user.home_cell_res9 == before
    assert user.home_changed_at is None


async def test_explore_is_held_in_redis_with_a_ttl(
    auth_client: httpx.AsyncClient,
    redis_client: Redis,
    settings: Settings,
    user: User,
) -> None:
    await auth_client.post("/v1/me/explore", json=KARATINA.as_payload())

    ttl = await redis_client.ttl(f"explore:{user.id}")

    assert 0 < ttl <= settings.explore_ttl_seconds


async def test_explore_changes_the_radius_anchor(
    auth_client: httpx.AsyncClient,
    redis_client: Redis,
    user: User,
) -> None:
    await auth_client.post("/v1/me/explore", json=KARATINA.as_payload())

    anchor = await location_service.resolve_anchor_cell(redis_client, user=user, mode="explore")

    assert anchor == cells.locate(*KARATINA.as_tuple()).cell_res8
    assert anchor != user.home_cell_res8


async def test_stopping_explore_returns_to_home(
    auth_client: httpx.AsyncClient, redis_client: Redis, user: User
) -> None:
    await auth_client.post("/v1/me/explore", json=KARATINA.as_payload())

    response = await auth_client.delete("/v1/me/explore")

    assert response.status_code == 204
    anchor = await location_service.resolve_anchor_cell(redis_client, user=user, mode="explore")
    assert anchor == user.home_cell_res8


async def test_stopping_explore_when_not_exploring_succeeds(
    auth_client: httpx.AsyncClient,
) -> None:
    assert (await auth_client.delete("/v1/me/explore")).status_code == 204


async def test_an_expired_explore_falls_back_to_home(redis_client: Redis, user: User) -> None:
    """What a user would expect after leaving the app for an afternoon."""
    anchor = await location_service.resolve_anchor_cell(redis_client, user=user, mode="explore")

    assert anchor == user.home_cell_res8


async def test_others_see_the_home_area_not_the_explored_one(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    settings: Settings,
    user: User,
    authenticate: Authenticate,
) -> None:
    """Rule 3 of decision 1.1. A visitor is still labelled with where they live."""
    await make_area(db_session, name="Kamakwa", centre=NYERI_TOWN, width_km=3.0)
    await make_area(db_session, name="Karatina Centre", centre=KARATINA, width_km=3.0)
    await set_registered_density(db_session, at=NYERI_TOWN, registered=30)
    await set_registered_density(db_session, at=KARATINA, registered=30)
    await redis_client.flushdb()

    await auth_client.post("/v1/me/explore", json=KARATINA.as_payload())

    observer = await make_user(db_session, at=offset_km(NYERI_TOWN, north=1.0))
    observer_client = await authenticate(observer)
    body = (await observer_client.get(f"/v1/users/{user.id}")).json()

    assert body["area_label"]["label"] == "Kamakwa"


# ---------------------------------------------------------------------------
# GET /v1/me/radius
# ---------------------------------------------------------------------------
async def test_my_radius_lists_every_feature(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.get("/v1/me/radius")

    assert response.status_code == 200
    body = response.json()
    features = {entry["feature"] for entry in body["radii"]}
    assert features == {"feed", "groups", "marketplace", "services", "events"}


async def test_my_radius_is_rounded_for_display(
    auth_client: httpx.AsyncClient, settings: Settings
) -> None:
    """So the app shows "3 km", not a figure that reveals how dense the caller's
    own street is."""
    body = (await auth_client.get("/v1/me/radius")).json()

    for entry in body["radii"]:
        remainder = round(entry["radius_km"] / settings.radius_display_rounding_km, 6)
        assert remainder == int(remainder), entry


async def test_my_radius_reports_the_feed_has_no_filter(
    auth_client: httpx.AsyncClient,
) -> None:
    body = (await auth_client.get("/v1/me/radius")).json()

    feed = next(entry for entry in body["radii"] if entry["feature"] == "feed")
    marketplace = next(entry for entry in body["radii"] if entry["feature"] == "marketplace")

    assert feed["user_filter_max_km"] is None
    assert marketplace["user_filter_max_km"] == 50.0


async def test_an_empty_neighbourhood_reports_the_maximum(
    auth_client: httpx.AsyncClient, settings: Settings
) -> None:
    """Expected at launch, and the brief says so, which is why it is a documented
    response rather than an error."""
    body = (await auth_client.get("/v1/me/radius")).json()

    feed = next(entry for entry in body["radii"] if entry["feature"] == "feed")
    assert feed["radius_km"] == settings.feed_radius_max_km
    assert feed["at_maximum"] is True


async def test_a_user_with_no_location_still_gets_an_answer(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    """Defaults rather than an error, so the app has nothing to special-case."""
    nowhere = await make_user(db_session)
    nowhere_client = await authenticate(nowhere)

    response = await nowhere_client.get("/v1/me/radius")

    assert response.status_code == 200
    body = response.json()
    assert body["has_location"] is False
    assert body["active_neighbours_estimate"] == 0


async def test_my_radius_can_be_measured_from_an_explore_anchor(
    auth_client: httpx.AsyncClient,
) -> None:
    await auth_client.post("/v1/me/explore", json=KARATINA.as_payload())

    response = await auth_client.get("/v1/me/radius?anchor=explore")

    assert response.status_code == 200
    assert response.json()["anchor"] == "explore"


async def test_an_unknown_anchor_is_refused(auth_client: httpx.AsyncClient) -> None:
    assert (await auth_client.get("/v1/me/radius?anchor=elsewhere")).status_code == 422


async def test_the_neighbour_estimate_is_rounded(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    """An exact count would be a count of identifiable people."""
    await set_cell_density(db_session, origin=NYERI_TOWN, active_per_cell=37, rings=6)
    await redis_client.flushdb()

    body = (await auth_client.get("/v1/me/radius")).json()

    assert body["active_neighbours_estimate"] % 10 == 0


async def test_my_radius_requires_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/me/radius")).status_code == 401


# ---------------------------------------------------------------------------
# Areas
# ---------------------------------------------------------------------------
async def test_areas_are_listed_without_geometry(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A boundary plus a label is a sharper location than a label alone, and it
    would be a large payload on expensive data."""
    await make_area(db_session, name="Kamakwa", centre=NYERI_TOWN)

    response = await auth_client.get("/v1/areas")

    assert response.status_code == 200
    body = response.json()
    assert body[0]["name"] == "Kamakwa"
    assert "boundary" not in body[0]
    assert "geometry" not in body[0]


async def test_areas_can_be_filtered(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_area(db_session, name="Kamakwa", centre=NYERI_TOWN, kind="estate")
    await make_area(db_session, name="Karatina", centre=KARATINA, kind="town", town=None)

    estates = (await auth_client.get("/v1/areas?kind=estate")).json()

    assert [area["name"] for area in estates] == ["Kamakwa"]


async def test_areas_require_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/areas")).status_code == 401
