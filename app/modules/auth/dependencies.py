"""Authentication dependencies.

`CurrentUser` is how every protected endpoint gets its caller. It does four things
on each request: verifies the access token, confirms the named device session is
still live, loads the user, and records that the app was opened.

The session check is the part that costs something — one indexed lookup per
request — and it is worth it. Without it, "sign out this device" would not take
effect until the access token expired, so a stolen phone would keep working for
another fifteen minutes after the owner revoked it. Fifteen minutes is exactly
when it matters.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.errors import ForbiddenError, RateLimitedError, UnauthenticatedError
from app.core.ratelimit import get_rate_limiter
from app.core.security import AccessTokenClaims, decode_access_token
from app.modules.auth import service as auth_service
from app.modules.auth.models import DeviceSession
from app.modules.users.models import User, UserStatus
from app.modules.users.service import get_user, touch_last_active

logger = logging.getLogger(__name__)

AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "


def _extract_bearer_token(request: Request) -> str | None:
    header = request.headers.get(AUTHORIZATION_HEADER)
    if not header or not header.startswith(BEARER_PREFIX):
        return None
    token = header[len(BEARER_PREFIX) :].strip()
    return token or None


@dataclass(frozen=True, slots=True)
class Authenticated:
    user: User
    device_session: DeviceSession
    claims: AccessTokenClaims


async def authenticate_access_token(
    session: AsyncSession, *, token: str, settings: Settings
) -> Authenticated:
    """Verify an access token and everything it names, or `UnauthenticatedError`.

    Shared by HTTP requests and the WebSocket, so the two cannot drift apart on
    what counts as signed in.
    """
    claims = decode_access_token(token, settings)

    device_session = await auth_service.get_session_for_access(
        session, session_id=claims.session_id
    )
    if device_session is None or device_session.revoked_at is not None:
        # Deliberately the same message as an invalid token: whether a session was
        # revoked or never existed is not the caller's business.
        raise UnauthenticatedError("That session is no longer valid")
    if device_session.user_id != claims.user_id:
        # A token whose session belongs to someone else means either a forged
        # token or a serious bug. Either way, refuse and make noise.
        logger.error("access_token_session_mismatch", extra={"session_id": str(claims.session_id)})
        raise UnauthenticatedError("That session is no longer valid")

    user = await get_user(session, claims.user_id)
    if user is None:
        raise UnauthenticatedError("That account no longer exists")
    if user.status == UserStatus.banned:
        raise ForbiddenError("This account has been banned")
    return Authenticated(user=user, device_session=device_session, claims=claims)


async def get_current_user(
    request: Request,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """The authenticated caller, or `UnauthenticatedError`."""
    token = _extract_bearer_token(request)
    if token is None:
        raise UnauthenticatedError("An access token is required")

    authenticated = await authenticate_access_token(session, token=token, settings=settings)
    user, device_session = authenticated.user, authenticated.device_session

    # Published for the rate limiter, which counts per account when it can,
    # and for routes that need to know which session is the caller's own —
    # both would otherwise re-decode a token this dependency just verified.
    request.state.user_id = str(user.id)
    request.state.session_id = device_session.id

    await touch_last_active(session, user.id)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_optional_user(
    request: Request,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> User | None:
    """The caller if they are signed in, otherwise None.

    For endpoints that behave differently rather than refusing — public profiles,
    and later the areas list. A bad token is treated as absent rather than as an
    error, so a client with a stale token still sees public content.
    """
    if _extract_bearer_token(request) is None:
        return None
    try:
        return await get_current_user(request, session, settings)
    except (UnauthenticatedError, ForbiddenError):
        return None


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


async def get_active_user(user: CurrentUser) -> User:
    """The caller, refused if their account is restricted.

    Used on endpoints that create or change things. Suspended accounts keep read
    access on purpose: they are still entitled to see why they were suspended and
    to export their data, and taking that away would be a punishment nobody chose.
    """
    if user.status == UserStatus.suspended:
        raise ForbiddenError(
            "This account is suspended and cannot post or send messages",
            details={"status": str(user.status)},
        )
    if user.status == UserStatus.deletion_pending:
        raise ForbiddenError(
            "This account is scheduled for deletion. Cancel the request to continue.",
            details={"status": str(user.status)},
        )
    return user


ActiveUser = Annotated[User, Depends(get_active_user)]


def user_rate_limit(
    *,
    bucket: str,
    limit: int,
    window_seconds: int,
    cost: int = 1,
    fail_open: bool | None = None,
) -> Callable[[User, Response], Awaitable[None]]:
    """Per-account rate limit for an authenticated route.

    This exists rather than `core.ratelimit.rate_limit(scope="user")` because of
    dependency ordering: route-level dependencies resolve before path parameters,
    so a limiter that reads `request.state.user_id` would run before
    authentication had set it and silently fall back to per-IP counting. Depending
    on `CurrentUser` here makes the order explicit instead of accidental.
    """

    async def dependency(user: CurrentUser, response: Response) -> None:
        limiter = get_rate_limiter()
        result = await limiter.hit(
            bucket,
            f"u:{user.id}",
            limit=limit,
            window_seconds=window_seconds,
            cost=cost,
            fail_open=fail_open,
        )
        if not result.allowed:
            logger.info("rate_limited", extra={"bucket": bucket, "scope": "user"})
            raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)
        response.headers.update(result.headers)

    return dependency


def current_session_id(request: Request) -> uuid.UUID | None:
    """The device session the caller's access token names.

    Published by `get_current_user`, so this is a plain attribute read. Returns
    None on an unauthenticated request rather than raising, which lets the
    sessions list mark "this device" without a second code path.
    """
    session_id = getattr(request.state, "session_id", None)
    return session_id if isinstance(session_id, uuid.UUID) else None


def is_new_account(user: User, settings: Settings) -> bool:
    """Whether an account is still inside its probation window."""
    age = datetime.now(UTC) - user.created_at
    return age < timedelta(hours=settings.new_account_age_hours)


def tiered_user_rate_limit(
    *,
    bucket: str,
    window_seconds: int,
    new_account_limit: Callable[[Settings], int],
    established_limit: Callable[[Settings], int],
) -> Callable[[User, Response], Awaitable[None]]:
    """A per-account limit that is tighter while the account is new.

    A throwaway account is the cheapest spam vector there is, and age is the one
    signal that cannot be faked without waiting. So a brand-new account gets a
    handful of posts a day, and an established neighbour gets room to be useful.

    Depends on `ActiveUser`, not `CurrentUser`: everything this guards creates
    content, and a suspended or deletion-pending account must be refused before
    it is counted. Both buckets share one counter, so an account crossing the age
    threshold mid-window keeps what it has already used.
    """

    async def dependency(user: ActiveUser, response: Response) -> None:
        settings = get_settings()
        limit = (
            new_account_limit(settings)
            if is_new_account(user, settings)
            else established_limit(settings)
        )
        result = await get_rate_limiter().hit(
            bucket, f"u:{user.id}", limit=limit, window_seconds=window_seconds
        )
        if not result.allowed:
            logger.info(
                "rate_limited",
                extra={
                    "bucket": bucket,
                    "scope": "user",
                    "new_account": is_new_account(user, settings),
                },
            )
            raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)
        response.headers.update(result.headers)

    return dependency
