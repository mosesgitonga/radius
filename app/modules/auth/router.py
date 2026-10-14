"""Authentication endpoints.

Rate limits here are the strictest in the product and fail closed, because this
is the only place the backend spends money on an unauthenticated caller's behalf:
every OTP request is an SMS somebody pays for.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.errors import ConflictError
from app.core.providers.sms import SmsProvider, get_sms_provider
from app.core.ratelimit import RateLimiter, get_rate_limiter, rate_limit
from app.core.redis import get_redis
from app.modules.auth import service as auth_service
from app.modules.auth.dependencies import CurrentUser, current_session_id
from app.modules.auth.phone import mask_for_display, normalise_phone_number
from app.modules.auth.schemas import (
    LogoutAllIn,
    LogoutAllOut,
    LogoutIn,
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    OtpVerifyOut,
    RefreshIn,
    SessionOut,
    SignupIn,
    SignupOut,
    TokenPairOut,
)
from app.modules.users import service as users_service
from app.modules.users.models import ConsentType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SmsDep = Annotated[SmsProvider, Depends(get_sms_provider)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def _to_token_pair(pair: auth_service.TokenPair) -> TokenPairOut:
    return TokenPairOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in_seconds=pair.access_expires_in_seconds,
    )


@router.post(
    "/otp/request",
    response_model=OtpRequestOut,
    summary="Request a verification code",
    description=(
        "Sends a one-time code by SMS. The response is identical whether or not "
        "the number already has an account, so this endpoint cannot be used to "
        "discover who is registered."
    ),
    dependencies=[
        Depends(
            rate_limit(
                bucket="otp_request_ip",
                limit=lambda settings: settings.otp_daily_cap_per_ip,
                window_seconds=86_400,
                scope="ip",
                # Fails closed: without a working limiter this endpoint sends real
                # SMS at the project's expense to anyone who asks.
                fail_open=False,
            )
        )
    ],
)
async def request_otp(
    payload: OtpRequestIn,
    redis: RedisDep,
    sms: SmsDep,
    limiter: LimiterDep,
    settings: SettingsDep,
) -> OtpRequestOut:
    phone_number = normalise_phone_number(payload.phone_number)
    challenge = await auth_service.request_otp(
        redis, sms, limiter, phone_number=phone_number, settings=settings
    )
    return OtpRequestOut(
        expires_in_seconds=challenge.expires_in_seconds,
        resend_after_seconds=challenge.resend_after_seconds,
        phone_number_masked=mask_for_display(phone_number),
    )


@router.post(
    "/otp/verify",
    response_model=OtpVerifyOut,
    summary="Verify a code",
    description=(
        "Checks the code. An existing account receives tokens; a new number "
        "receives a single-use `signup_token` for `POST /v1/auth/signup`."
    ),
    dependencies=[
        Depends(
            rate_limit(
                bucket="otp_verify_ip",
                limit=30,
                window_seconds=3600,
                scope="ip",
                fail_open=False,
            )
        )
    ],
)
async def verify_otp(
    payload: OtpVerifyIn,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> OtpVerifyOut:
    phone_number = normalise_phone_number(payload.phone_number)
    verified = await auth_service.verify_otp(
        session,
        redis,
        phone_number=phone_number,
        code=payload.code,
        settings=settings,
        device_name=payload.device_name,
        device_platform=payload.device_platform,
    )

    if verified.tokens is not None:
        return OtpVerifyOut(status="signed_in", tokens=_to_token_pair(verified.tokens))
    return OtpVerifyOut(status="signup_required", signup_token=verified.signup_token)


@router.post(
    "/signup",
    response_model=SignupOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    description=(
        "Completes signup for a number already verified by OTP. Requires an 18+ "
        "confirmation and explicit consent to the terms, privacy policy, and "
        "location use; marketing consent is optional."
    ),
    dependencies=[
        Depends(
            rate_limit(
                bucket="signup_ip",
                limit=10,
                window_seconds=86_400,
                scope="ip",
                fail_open=False,
            )
        )
    ],
)
async def signup(
    payload: SignupIn,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> SignupOut:
    phone_number = await auth_service.consume_signup_token(
        redis, token=payload.signup_token, settings=settings
    )

    # The token is single-use and already consumed, so a race between two
    # simultaneous signups cannot create two accounts. This covers the other
    # order: a number that gained an account between verify and signup.
    if await users_service.get_user_by_phone(session, phone_number) is not None:
        raise ConflictError("An account already exists for that number")

    granted = {
        ConsentType.terms,
        ConsentType.privacy_policy,
        ConsentType.location_use,
    }
    if payload.accept_marketing:
        granted.add(ConsentType.marketing)

    user = await users_service.create_user(
        session,
        phone_number=phone_number,
        display_name=payload.display_name,
        settings=settings,
        granted_consents=granted,
    )
    tokens = await auth_service.issue_tokens(
        session,
        user=user,
        settings=settings,
        device_name=payload.device_name,
        device_platform=payload.device_platform,
    )
    return SignupOut(tokens=_to_token_pair(tokens), user_id=user.id)


@router.post(
    "/refresh",
    response_model=TokenPairOut,
    summary="Exchange a refresh token",
    description=(
        "Rotates the refresh token and issues a new access token. Presenting a "
        "token that has already been rotated revokes every session descended from "
        "the same sign-in, because a replay and a client that lost the response "
        "are indistinguishable."
    ),
    dependencies=[
        Depends(rate_limit(bucket="refresh_ip", limit=120, window_seconds=3600, scope="ip"))
    ],
)
async def refresh(payload: RefreshIn, session: SessionDep, settings: SettingsDep) -> TokenPairOut:
    tokens = await auth_service.rotate_refresh_token(
        session,
        refresh_token=payload.refresh_token,
        settings=settings,
        device_name=payload.device_name,
        device_platform=payload.device_platform,
    )
    return _to_token_pair(tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out this device",
    description=(
        "Revokes the session behind the given refresh token. Succeeds even if the "
        "token is already gone, so a client retrying on a flaky connection is not "
        "left unable to complete a sign-out."
    ),
)
async def logout(payload: LogoutIn, session: SessionDep, settings: SettingsDep) -> None:
    await auth_service.logout(session, refresh_token=payload.refresh_token, settings=settings)


@router.post(
    "/logout-all",
    response_model=LogoutAllOut,
    summary="Sign out everywhere",
    description="Revokes every session for the account, optionally keeping this one.",
)
async def logout_all(
    payload: LogoutAllIn,
    user: CurrentUser,
    request: Request,
    session: SessionDep,
) -> LogoutAllOut:
    keep = current_session_id(request) if payload.keep_current_session else None
    revoked = await auth_service.logout_all(session, user=user, keep_session_id=keep)
    return LogoutAllOut(sessions_revoked=revoked)


@router.get(
    "/sessions",
    response_model=list[SessionOut],
    summary="List signed-in devices",
    description=(
        "Every live session for the account, newest first, so a user can spot a "
        "device they do not recognise."
    ),
)
async def list_sessions(
    user: CurrentUser, request: Request, session: SessionDep
) -> list[SessionOut]:
    current_id = current_session_id(request)
    sessions = await auth_service.list_sessions(session, user_id=user.id)
    return [
        SessionOut(
            id=device.id,
            device_name=device.device_name,
            device_platform=device.device_platform,
            created_at=device.created_at,
            last_used_at=device.last_used_at,
            expires_at=device.expires_at,
            is_current=device.id == current_id,
        )
        for device in sessions
    ]


__all__ = ["router"]
