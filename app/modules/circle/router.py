"""Circle endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.core.redis import get_redis
from app.modules.auth.dependencies import ActiveUser, CurrentUser
from app.modules.circle import service as circle_service
from app.modules.circle.models import CircleRequest
from app.modules.circle.schemas import (
    CircleMemberOut,
    CircleMemberPageOut,
    CircleRequestIn,
    CircleRequestOut,
    CircleRequestPageOut,
    CircleRequestSentOut,
)
from app.modules.feed.views import author_views_for, load_users
from app.modules.location import cells
from app.modules.location.service import area_label_for_cells, town_label_for_cell
from app.modules.users.models import User
from app.modules.users.schemas import AreaLabelOut

router = APIRouter(tags=["circle"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]


async def _request_views(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    viewer: User,
    requests: Sequence[CircleRequest],
    settings: Settings,
) -> list[CircleRequestOut]:
    def other(request: CircleRequest) -> uuid.UUID:
        return request.recipient_id if request.requester_id == viewer.id else request.requester_id

    people = await load_users(session, (other(request) for request in requests))
    cards = await author_views_for(session, storage, list(people.values()))
    lifetime = timedelta(days=settings.circle_request_expiry_days)
    return [
        CircleRequestOut(
            id=request.id,
            user=cards[other(request)],
            direction="outgoing" if request.requester_id == viewer.id else "incoming",
            created_at=request.created_at,
            expires_at=request.created_at + lifetime,
        )
        for request in requests
        if other(request) in cards
    ]


@router.post(
    "/circle/requests",
    response_model=CircleRequestSentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ask someone to join my circle",
    description=(
        "People in each other's circle see what each other shares beyond the "
        "neighbourhood, and message without a request, wherever they live.\n\n"
        "- **Idempotent:** asking again while your request is waiting returns it; "
        "asking someone already in your circle is not an error.\n"
        "- **Crossed requests:** if they had already asked you, this accepts theirs "
        "and you are both in each other's circle.\n"
        "- **Refusals all look alike:** the other person's setting, a recent decline or "
        "expiry (`CIRCLE_REQUEST_COOLDOWN_DAYS`), or their full circle all give 403 "
        "`details.reason = circle_request_not_allowed`. A block either way is a 404.\n"
        "- **The contact rule** (decision M.7): until you have been at home on enough "
        "days, you can ask only people you already have an accepted conversation with "
        "(403 `contact_locked`).\n"
        "- **Limits:** `CIRCLE_REQUESTS_PER_DAY` a day (fewer for accounts under "
        "`CIRCLE_NEW_ACCOUNT_DAYS`), at most `CIRCLE_MAX_PENDING_OUTGOING` waiting "
        "(409 `too_many_pending`), and `CIRCLE_MAX_MEMBERS` in a circle "
        "(409 `circle_full`)."
    ),
)
async def send_request(
    payload: CircleRequestIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> CircleRequestSentOut:
    outcome = await circle_service.send_request(
        session, redis, requester=user, recipient_id=payload.user_id, settings=settings
    )
    rendered = (
        await _request_views(
            session, storage, viewer=user, requests=[outcome.request], settings=settings
        )
        if outcome.request is not None
        and outcome.relationship == circle_service.Relationship.request_sent
        else []
    )
    return CircleRequestSentOut(
        relationship=outcome.relationship, request=rendered[0] if rendered else None
    )


@router.get(
    "/circle/requests",
    response_model=CircleRequestPageOut,
    summary="Circle requests",
    description=(
        "`incoming`: requests waiting for my answer. `outgoing`: requests I sent that "
        "are still waiting, as far as I can tell; a request the other person declined "
        "is shown exactly like one they have not answered, until it expires. Newest "
        "first."
    ),
)
async def list_requests(
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
    params: PageDep,
    direction: Annotated[Literal["incoming", "outgoing"], Query()] = "incoming",
) -> CircleRequestPageOut:
    requests, next_cursor = await circle_service.list_requests(
        session, user=user, direction=direction, params=params, settings=settings
    )
    return CircleRequestPageOut(
        items=await _request_views(
            session, storage, viewer=user, requests=requests, settings=settings
        ),
        next_cursor=next_cursor,
    )


@router.post(
    "/circle/requests/{request_id}/accept",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Accept a circle request",
    description="The person who asked is told. 409 `circle_full` if your circle is full.",
)
async def accept_request(
    request_id: uuid.UUID, user: ActiveUser, session: SessionDep, settings: SettingsDep
) -> None:
    await circle_service.accept_request(
        session, user=user, request_id=request_id, settings=settings
    )


@router.post(
    "/circle/requests/{request_id}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Decline a circle request",
    description=(
        "Silent: the person who asked is not told, and sees their request as waiting "
        "until it would have expired. They cannot ask again for "
        "`CIRCLE_REQUEST_COOLDOWN_DAYS`."
    ),
)
async def decline_request(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> None:
    await circle_service.decline_request(
        session, user=user, request_id=request_id, settings=settings
    )


@router.delete(
    "/circle/requests/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw a circle request I sent",
    description=(
        "The other person stops seeing it. One they had already declined stays "
        "declined, so its cooldown still holds."
    ),
)
async def cancel_request(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> None:
    await circle_service.cancel_request(
        session, user=user, request_id=request_id, settings=settings
    )


@router.get(
    "/circle",
    response_model=CircleMemberPageOut,
    summary="My circle",
    description=(
        "Most recently added first. Someone within my feed radius shows their area and "
        "a distance band; anyone beyond it, their town only and no distance. No "
        "last-seen time or online status, and a move is never announced. Always from "
        "my home: exploring elsewhere changes nothing here."
    ),
)
async def list_circle(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    params: PageDep,
) -> CircleMemberPageOut:
    members, next_cursor = await circle_service.list_members(session, user=user, params=params)
    people = await load_users(session, (member.user_id for member in members))
    cards = await author_views_for(session, storage, list(people.values()))
    within = await circle_service.homes_within_feed(
        session, redis, viewer=user, user_ids=list(people), settings=settings
    )

    items: list[CircleMemberOut] = []
    for member in members:
        person = people.get(member.user_id)
        if person is None:  # pragma: no cover — links cascade with their users
            continue
        near = person.id in within
        label: AreaLabelOut | None = None
        bucket = None
        if person.home_cell_res9 is not None:
            found = (
                await area_label_for_cells(session, redis, user=person, settings=settings)
                if near
                else await town_label_for_cell(
                    session, redis, cell_res9=person.home_cell_res9, settings=settings
                )
            )
            label = AreaLabelOut(label=found.label, precision=found.precision)
            if near and user.home_cell_res8 is not None and person.home_cell_res8 is not None:
                bucket = cells.bucket_between_cells(user.home_cell_res8, person.home_cell_res8)
        items.append(
            CircleMemberOut(
                user=cards[person.id],
                area_label=label,
                distance_bucket=bucket,
                within_feed_radius=near,
                since=member.since,
            )
        )
    return CircleMemberPageOut(items=items, next_cursor=next_cursor)


@router.delete(
    "/circle/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove someone from my circle",
    description=(
        "Silent: they are not told. Removing someone not in your circle is not an "
        "error. A conversation that was accepted stays accepted."
    ),
)
async def remove_member(user_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await circle_service.remove_member(session, user=user, other_id=user_id)
