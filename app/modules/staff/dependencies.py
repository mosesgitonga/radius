"""Who is calling the admin API.

`CurrentStaff` is the only way into `/v1/admin`. It accepts a staff token and
nothing else: a user access token fails to decode as one (wrong type, no staff
audience), so a neighbour's account — however it was obtained — never reaches a
moderation endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.errors import ForbiddenError, UnauthenticatedError
from app.modules.staff import service as staff_service
from app.modules.staff.models import StaffAccount, StaffRole, StaffSession

BEARER_PREFIX = "Bearer "


@dataclass(frozen=True, slots=True)
class StaffCaller:
    account: StaffAccount
    session: StaffSession


async def get_staff_caller(
    request: Request,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StaffCaller:
    header = request.headers.get("Authorization", "")
    token = header[len(BEARER_PREFIX) :].strip() if header.startswith(BEARER_PREFIX) else ""
    if not token:
        raise UnauthenticatedError("A staff token is required")
    account, staff_session = await staff_service.authenticate(
        session, token=token, settings=settings
    )
    # For the rate limiter, which counts per caller when it can.
    request.state.user_id = f"staff:{account.id}"
    return StaffCaller(account=account, session=staff_session)


StaffCallerDep = Annotated[StaffCaller, Depends(get_staff_caller)]


async def get_current_staff(caller: StaffCallerDep) -> StaffAccount:
    return caller.account


CurrentStaff = Annotated[StaffAccount, Depends(get_current_staff)]


async def get_admin(staff: CurrentStaff) -> StaffAccount:
    if staff.role != StaffRole.admin:
        raise ForbiddenError("That needs an admin")
    return staff


AdminStaff = Annotated[StaffAccount, Depends(get_admin)]
