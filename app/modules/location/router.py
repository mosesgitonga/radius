"""Location endpoints: home, explore, radius, areas."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings, get_settings
from app.core.db import get_session
from app.core.redis import get_redis
from app.modules.auth.dependencies import ActiveUser, CurrentUser, user_rate_limit
from app.modules.location import service as location_service
from app.modules.location.models import Area
from app.modules.location.radius import RadiusEngine
from app.modules.location.schemas import (
    AreaOut,
    ExploreOut,
    HomeLocationOut,
    LocationIn,
    MyRadiusOut,
    RadiusOut,
)
from app.modules.users.schemas import AreaLabelOut

router = APIRouter(tags=["location"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]


@router.put(
    "/me/location",
    response_model=HomeLocationOut,
    summary="Set my home area",
    description=(
        "Converts the coordinates to an H3 cell and stores only the cell and its "
        "centre. The coordinates are not kept, not logged, and never returned.\n\n"
        "Changing an established home area is limited to roughly once a fortnight, "
        "which stops someone hopping between neighbourhoods to spam each in turn "
        "while leaving a student who moves between campus and home free to switch."
    ),
    dependencies=[
        Depends(
            # Deliberately tight. A client with a location bug could otherwise
            # write on every GPS tick, and each write is a spoof check and an
            # area resolution.
            user_rate_limit(bucket="set_location", limit=20, window_seconds=3600)
        )
    ],
)
async def set_home_location(
    payload: LocationIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> HomeLocationOut:
    outcome = await location_service.set_home_location(
        session,
        redis,
        user=user,
        latitude=payload.latitude,
        longitude=payload.longitude,
        settings=settings,
    )

    next_change = None
    if user.home_changed_at is not None and settings.home_change_cooldown_days > 0:
        next_change = user.home_changed_at + timedelta(days=settings.home_change_cooldown_days)

    # `outcome.spoof_suspected` is deliberately not returned. Telling a caller
    # their jump looked implausible just teaches them what to avoid next time.
    return HomeLocationOut(
        area_label=AreaLabelOut(
            label=outcome.area_label.label, precision=outcome.area_label.precision
        ),
        is_first_time=outcome.is_first_time,
        next_change_allowed_at=next_change,
    )


@router.post(
    "/me/explore",
    response_model=ExploreOut,
    summary="Explore somewhere else",
    description=(
        "Temporarily anchors browsing to another location. The anchor lives in "
        "memory for about two hours and is never written to the database.\n\n"
        "This changes only what the caller sees. Anything they post still carries "
        "their home area, and other people still see their home area — so explore "
        "mode cannot be used to post into a neighbourhood somebody does not live in."
    ),
    dependencies=[Depends(user_rate_limit(bucket="start_explore", limit=30, window_seconds=3600))],
)
async def start_explore(
    payload: LocationIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> ExploreOut:
    explored = await location_service.start_explore(
        redis,
        user_id=user.id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        settings=settings,
    )
    label = await location_service.area_label_for_cell(
        session, redis, cell_res9=explored.cell_res9, settings=settings
    )
    return ExploreOut(
        area_label=AreaLabelOut(label=label.label, precision=label.precision),
        expires_at=explored.expires_at,
    )


@router.delete(
    "/me/explore",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop exploring",
    description="Returns browsing to the caller's home area. Succeeds if not exploring.",
)
async def stop_explore(user: CurrentUser, redis: RedisDep) -> None:
    await location_service.stop_explore(redis, user_id=user.id)


@router.get(
    "/me/radius",
    response_model=MyRadiusOut,
    summary="My current radius, per feature",
    description=(
        "What 'nearby' currently means for this account. The radius adapts to how "
        "many active neighbours are around: smaller where it is busy, wider where "
        "it is quiet, within each feature's limits.\n\n"
        "Rounded to the nearest half kilometre, so the app can say 'Your radius: "
        "3 km' without the exact figure revealing how dense the caller's own "
        "street is."
    ),
)
async def get_my_radius(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    anchor: Annotated[
        str,
        Query(
            pattern="^(home|explore)$",
            description="Measure from the home area, or from an active explore session.",
        ),
    ] = "home",
) -> MyRadiusOut:
    anchor_cell = await location_service.resolve_anchor_cell(
        redis, user=user, mode="explore" if anchor == "explore" else "home"
    )

    if anchor_cell is None:
        # No location yet. Report each feature's maximum, which is what a new user
        # would get the moment they set one, rather than erroring or returning
        # zeros that the app would have to special-case.
        return MyRadiusOut(
            anchor=anchor,
            has_location=False,
            active_neighbours_estimate=0,
            radii=[
                RadiusOut(
                    feature=feature,
                    radius_km=rule.max_km,
                    user_filter_max_km=rule.user_filter_max_km,
                    at_maximum=True,
                    at_minimum=False,
                )
                for feature, rule in settings.radius_rules.items()
            ],
        )

    engine = RadiusEngine(session, redis, settings)
    results = await engine.all_radii(anchor_cell)
    feed = results[RadiusFeature.feed]

    return MyRadiusOut(
        anchor=anchor,
        has_location=True,
        # Rounded to the nearest ten so it reads as an estimate about the area
        # rather than an exact count of identifiable people.
        active_neighbours_estimate=round(feed.active_users_counted, -1),
        radii=[
            RadiusOut(
                feature=feature,
                radius_km=result.display_km(settings.radius_display_rounding_km),
                user_filter_max_km=engine.user_filter_cap_km(feature),
                at_maximum=result.clamped_to_maximum,
                at_minimum=result.clamped_to_minimum,
            )
            for feature, result in results.items()
        ],
    )


@router.get(
    "/areas",
    response_model=list[AreaOut],
    summary="Seeded areas",
    description=(
        "Named areas, for pickers and filters. Boundary geometry is deliberately "
        "not included: it would be a large payload on expensive data, and a "
        "boundary plus a label is a sharper location than a label alone."
    ),
)
async def list_areas(
    user: CurrentUser,
    session: SessionDep,
    town: Annotated[str | None, Query(max_length=120)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[AreaOut]:
    statement = select(Area).where(Area.is_active.is_(True))
    if town:
        statement = statement.where(Area.town == town)
    if kind:
        statement = statement.where(Area.kind == kind)
    statement = statement.order_by(Area.name.asc()).limit(limit)

    areas = (await session.execute(statement)).scalars().all()
    return [
        AreaOut(
            id=area.id,
            name=area.name,
            kind=area.kind,
            town=area.town,
            county=area.county,
        )
        for area in areas
    ]
