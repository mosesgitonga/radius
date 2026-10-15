"""Staff sign-in and staff management, under `/admin`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.errors import UnauthenticatedError
from app.core.ratelimit import rate_limit
from app.modules.staff import service as staff_service
from app.modules.staff.dependencies import AdminStaff, CurrentStaff, StaffCallerDep
from app.modules.staff.schemas import (
    IssuedCredentialsOut,
    PasswordChangeIn,
    SignInIn,
    SignInOut,
    StaffCreateIn,
    StaffOut,
    StaffUpdateIn,
)
from app.modules.staff.service import IssuedCredentials

router = APIRouter(prefix="/admin", tags=["admin: staff"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _issued(issued: IssuedCredentials) -> IssuedCredentialsOut:
    return IssuedCredentialsOut(
        staff=StaffOut.of(issued.account, now=datetime.now(UTC)),
        temporary_password=issued.password,
        totp_secret=issued.totp_secret,
        totp_uri=issued.totp_uri,
    )


@router.post(
    "/auth/sign-in",
    response_model=SignInOut,
    summary="Sign in as staff",
    description=(
        "Email, password and a code from the authenticator app, all at once. Every "
        "failure gets the same answer. Repeated failures lock the account for a "
        "while, and each code works only once."
    ),
    dependencies=[
        Depends(rate_limit(bucket="staff_sign_in", limit=10, window_seconds=900, scope="ip"))
    ],
)
async def sign_in(
    payload: SignInIn, request: Request, session: SessionDep, settings: SettingsDep
) -> SignInOut:
    try:
        signed_in = await staff_service.sign_in(
            session,
            email=payload.email,
            password=payload.password,
            code=payload.code,
            ip_address=getattr(request.state, "client_ip", None),
            settings=settings,
        )
    except UnauthenticatedError:
        # Keep the failure count and the audit row; the error still goes out.
        await session.commit()
        raise
    return SignInOut(
        access_token=signed_in.token,
        expires_at=signed_in.session.expires_at,
        staff=StaffOut.of(signed_in.account, now=datetime.now(UTC)),
    )


@router.post(
    "/auth/sign-out", status_code=status.HTTP_204_NO_CONTENT, summary="End this staff session"
)
async def sign_out(caller: StaffCallerDep, session: SessionDep) -> None:
    await staff_service.sign_out(session, caller.session)


@router.get("/me", response_model=StaffOut, summary="The signed-in staff member")
async def me(staff: CurrentStaff) -> StaffOut:
    return StaffOut.of(staff, now=datetime.now(UTC))


@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change your password",
    description="Needs the current password and a fresh authenticator code.",
    dependencies=[
        Depends(rate_limit(bucket="staff_password", limit=5, window_seconds=900, scope="ip"))
    ],
)
async def change_password(
    payload: PasswordChangeIn, staff: CurrentStaff, session: SessionDep, settings: SettingsDep
) -> None:
    await staff_service.change_password(
        session,
        account=staff,
        current_password=payload.current_password,
        new_password=payload.new_password,
        code=payload.code,
        settings=settings,
    )


@router.get("/staff", response_model=list[StaffOut], summary="List staff accounts (admin)")
async def list_staff(admin: AdminStaff, session: SessionDep) -> list[StaffOut]:
    now = datetime.now(UTC)
    return [StaffOut.of(account, now=now) for account in await staff_service.list_accounts(session)]


@router.post(
    "/staff",
    response_model=IssuedCredentialsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a staff account (admin)",
    description="Returns a temporary password and the authenticator secret, once.",
)
async def create_staff(
    payload: StaffCreateIn, admin: AdminStaff, session: SessionDep, settings: SettingsDep
) -> IssuedCredentialsOut:
    issued = await staff_service.create_account(
        session,
        email=payload.email,
        display_name=payload.display_name,
        role=payload.role,
        created_by=admin,
        settings=settings,
    )
    await session.flush()
    return _issued(issued)


@router.patch("/staff/{staff_id}", response_model=StaffOut, summary="Update a staff account")
async def update_staff(
    staff_id: uuid.UUID, payload: StaffUpdateIn, admin: AdminStaff, session: SessionDep
) -> StaffOut:
    account = await staff_service.require_account(session, staff_id)
    await staff_service.update_account(
        session,
        account=account,
        by=admin,
        role=payload.role,
        disabled=payload.disabled,
        display_name=payload.display_name,
    )
    await session.flush()
    return StaffOut.of(account, now=datetime.now(UTC))


@router.post(
    "/staff/{staff_id}/reset-credentials",
    response_model=IssuedCredentialsOut,
    summary="Issue a new password and authenticator secret (admin)",
    description="For a lost phone or a leak. Ends the account's sessions and unlocks it.",
)
async def reset_staff_credentials(
    staff_id: uuid.UUID, admin: AdminStaff, session: SessionDep, settings: SettingsDep
) -> IssuedCredentialsOut:
    account = await staff_service.require_account(session, staff_id)
    issued = await staff_service.reset_credentials(
        session, account=account, by=admin, settings=settings
    )
    await session.flush()
    return _issued(issued)
