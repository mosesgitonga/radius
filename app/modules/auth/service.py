"""Authentication: one-time codes, tokens, and device sessions.

Three parts worth reading before changing anything.

**OTP state lives in Redis, hashed.** A pending code is not durable data: it is
valid for five minutes and then worthless, so storing it in PostgreSQL would buy
nothing and leave rows to purge. It is stored as a keyed hash so that a Redis
snapshot does not hand over live codes, alongside its attempt counter so that
"wrong code five times" is enforced atomically rather than by a read followed by
a write.

**Verification does not reveal whether an account exists.** `request_otp` responds
identically for a registered and an unregistered number, and `verify_otp` returns
either a token pair or a signup token. Telling a caller which numbers are
registered turns this endpoint into a tool for enumerating the user base — and in
a neighbourhood product, that is a list of who lives in a place.

**Refresh tokens rotate, and reuse revokes the family.** See the docstring on
`DeviceSession` for why the family exists; `rotate_refresh_token` is where it is
enforced.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    RateLimitedError,
    UnauthenticatedError,
)
from app.core.providers.sms import SmsProvider
from app.core.ratelimit import RateLimiter
from app.core.redis import hash_get_all, hash_increment, key_ttl
from app.core.security import (
    create_access_token,
    expires_in_seconds,
    generate_numeric_code,
    generate_opaque_token,
    hash_secret,
    refresh_token_expiry,
    verify_secret,
)
from app.modules.auth.models import DeviceSession, SessionRevocationReason
from app.modules.moderation.audit import record_audit_event
from app.modules.moderation.models import AuditAction
from app.modules.users.models import User, UserStatus
from app.modules.users.service import touch_last_active

logger = logging.getLogger(__name__)

OTP_KEY_PREFIX: Final = "otp"
OTP_COOLDOWN_PREFIX: Final = "otp:cooldown"
SIGNUP_TOKEN_PREFIX: Final = "signup"  # noqa: S105 — a key prefix, not a secret

# Fields inside the Redis hash holding a pending code.
_FIELD_HASH: Final = "hash"
_FIELD_ATTEMPTS: Final = "attempts"


@dataclass(frozen=True, slots=True)
class OtpChallenge:
    """What the client is told after asking for a code.

    Carries no hint about whether the number belongs to an account.
    """

    expires_in_seconds: int
    resend_after_seconds: int


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_in_seconds: int
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class VerifiedPhone:
    """The result of a correct code.

    Exactly one of `tokens` and `signup_token` is set: an existing account gets
    signed in, a new number gets a short-lived ticket to finish signing up.
    """

    phone_number: str
    tokens: TokenPair | None
    signup_token: str | None

    @property
    def is_new_user(self) -> bool:
        return self.signup_token is not None


# ---------------------------------------------------------------------------
# One-time codes
# ---------------------------------------------------------------------------
def _otp_key(phone_number: str, settings: Settings) -> str:
    """Key the pending code by a hash of the number, not the number itself.

    Redis keys show up in slow logs, `MONITOR` output, and memory dumps. Hashing
    means none of those become a list of who is signing up.
    """
    return f"{OTP_KEY_PREFIX}:{hash_secret(phone_number, settings)}"


def _cooldown_key(phone_number: str, settings: Settings) -> str:
    return f"{OTP_COOLDOWN_PREFIX}:{hash_secret(phone_number, settings)}"


def _signup_key(token: str, settings: Settings) -> str:
    return f"{SIGNUP_TOKEN_PREFIX}:{hash_secret(token, settings)}"


async def request_otp(
    redis: Redis,
    sms: SmsProvider,
    limiter: RateLimiter,
    *,
    phone_number: str,
    settings: Settings,
) -> OtpChallenge:
    """Issue and send a code.

    Two limits apply here and one on the route. The per-IP daily cap is a route
    dependency; the per-number daily cap and the resend cooldown are enforced
    here, because only this layer knows the normalised number — a dependency
    would have to read and re-serialise the request body to find it.

    Both fail closed. An OTP endpoint whose limiter is broken spends real money
    sending real messages to someone who did not ask for them, so refusing is the
    cheaper failure.
    """
    daily = await limiter.hit(
        "otp_request_phone",
        f"phone:{hash_secret(phone_number, settings)}",
        limit=settings.otp_daily_cap_per_phone,
        window_seconds=86_400,
        fail_open=False,
    )
    if not daily.allowed:
        raise RateLimitedError(
            "This number has requested too many codes today",
            retry_after_seconds=daily.retry_after_seconds,
        )

    cooldown_key = _cooldown_key(phone_number, settings)
    remaining = await key_ttl(redis, cooldown_key)
    if remaining and remaining > 0:
        raise RateLimitedError(
            "A code was sent recently. Please wait before asking for another.",
            retry_after_seconds=int(remaining),
        )

    code = generate_numeric_code(settings.otp_length)
    key = _otp_key(phone_number, settings)

    # Replace rather than add to: asking for a new code invalidates the old one
    # and resets the attempt counter, so a user who mistyped three times is not
    # still two attempts from a lockout after requesting a fresh code.
    pipeline = redis.pipeline()
    pipeline.delete(key)
    pipeline.hset(
        key,
        mapping={_FIELD_HASH: hash_secret(code, settings), _FIELD_ATTEMPTS: 0},
    )
    pipeline.expire(key, settings.otp_ttl_seconds)
    pipeline.set(cooldown_key, "1", ex=settings.otp_resend_cooldown_seconds)
    await pipeline.execute()

    result = await sms.send(
        to=phone_number,
        body=(
            f"Your MyRadius code is {code}. "
            f"It expires in {settings.otp_ttl_seconds // 60} minutes. "
            "Do not share it with anyone."
        ),
    )
    if not result.accepted:
        # The code stays valid: the SMS may still arrive, and the client is told
        # to retry rather than being handed a code that was never usable.
        logger.error("otp_send_failed", extra={"provider": result.provider})

    logger.info("otp_requested", extra={"accepted": result.accepted})
    return OtpChallenge(
        expires_in_seconds=settings.otp_ttl_seconds,
        resend_after_seconds=settings.otp_resend_cooldown_seconds,
    )


async def verify_otp(
    session: AsyncSession,
    redis: Redis,
    *,
    phone_number: str,
    code: str,
    settings: Settings,
    device_name: str | None = None,
    device_platform: str | None = None,
) -> VerifiedPhone:
    """Check a code and either sign the user in or let them sign up."""
    key = _otp_key(phone_number, settings)
    stored = await hash_get_all(redis, key)
    if not stored:
        # One message for "never requested", "already used", and "expired". The
        # difference is only useful to someone probing numbers.
        raise UnauthenticatedError("That code is not valid or has expired")

    attempts = int(stored.get(_FIELD_ATTEMPTS, 0))
    if attempts >= settings.otp_max_attempts:
        await redis.delete(key)
        raise RateLimitedError(
            "Too many incorrect attempts. Please request a new code.",
            retry_after_seconds=settings.otp_resend_cooldown_seconds,
        )

    if not verify_secret(code, stored.get(_FIELD_HASH, ""), settings):
        # Incremented server-side so a client cannot avoid the count by dropping
        # the connection before reading the response.
        await hash_increment(redis, key, _FIELD_ATTEMPTS)
        remaining = max(0, settings.otp_max_attempts - attempts - 1)
        raise UnauthenticatedError(
            "That code is not correct", details={"attempts_remaining": remaining}
        )

    # Correct: consume it, so a captured code cannot be replayed.
    await redis.delete(key)

    user = await _find_active_user(session, phone_number)
    if user is None:
        signup_token = generate_opaque_token()
        await redis.set(
            _signup_key(signup_token, settings),
            phone_number,
            ex=settings.signup_token_ttl_seconds,
        )
        return VerifiedPhone(phone_number=phone_number, tokens=None, signup_token=signup_token)

    _assert_can_sign_in(user)
    tokens = await issue_tokens(
        session,
        user=user,
        settings=settings,
        device_name=device_name,
        device_platform=device_platform,
    )
    await touch_last_active(session, user.id)
    return VerifiedPhone(phone_number=phone_number, tokens=tokens, signup_token=None)


async def consume_signup_token(redis: Redis, *, token: str, settings: Settings) -> str:
    """Exchange a signup token for the phone number it verified.

    Single-use and deleted on read, so a replayed token cannot create a second
    account. The number lives in Redis rather than inside the token because a JWT
    is readable by anyone holding it, and there is no reason to put a phone number
    in the client's hands in a form it can be lifted out of.
    """
    key = _signup_key(token, settings)
    phone_number = await redis.getdel(key)
    if not phone_number:
        raise UnauthenticatedError(
            "That signup session has expired. Please verify your number again."
        )
    return str(phone_number)


async def _find_active_user(session: AsyncSession, phone_number: str) -> User | None:
    statement = select(User).where(User.phone_number == phone_number, User.deleted_at.is_(None))
    return (await session.execute(statement)).scalar_one_or_none()


def _assert_can_sign_in(user: User) -> None:
    """A banned account cannot sign in; a suspended one still can.

    Suspension restricts what a user may do, not whether they may look. Blocking
    sign-in entirely would also stop them reading why they were suspended or
    exporting their data, both of which they are entitled to.
    """
    if user.status == UserStatus.banned:
        raise ForbiddenError("This account has been banned")


# ---------------------------------------------------------------------------
# Sessions and tokens
# ---------------------------------------------------------------------------
async def issue_tokens(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
    device_name: str | None = None,
    device_platform: str | None = None,
    family_id: uuid.UUID | None = None,
) -> TokenPair:
    """Create a device session and its token pair.

    `family_id` is supplied when rotating, so the new session stays part of the
    chain that began at sign-in; omitted, a fresh family starts.
    """
    await _evict_oldest_sessions(session, user_id=user.id, settings=settings)

    now = datetime.now(UTC)
    refresh_token = generate_opaque_token()
    device_session = DeviceSession(
        user_id=user.id,
        refresh_token_hash=hash_secret(refresh_token, settings),
        family_id=family_id or uuid.uuid4(),
        device_name=device_name,
        device_platform=device_platform,
        expires_at=refresh_token_expiry(settings, now),
        last_used_at=now,
    )
    session.add(device_session)
    await session.flush()

    access_token, access_expires_at = create_access_token(
        user_id=user.id,
        session_id=device_session.id,
        settings=settings,
        now=now,
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_in_seconds=expires_in_seconds(access_expires_at, now),
        session_id=device_session.id,
    )


async def rotate_refresh_token(
    session: AsyncSession,
    *,
    refresh_token: str,
    settings: Settings,
    device_name: str | None = None,
    device_platform: str | None = None,
) -> TokenPair:
    """Exchange a refresh token for a new pair.

    The three failure cases are deliberately different:

    * **Unknown token** — never existed or the signing key rotated. Refused.
    * **Already consumed** — the token was rotated away. This is either a replay
      of a stolen token or an honest client that lost the response, and the two
      are indistinguishable, so the whole family is revoked. A thief loses the
      session they stole; the honest user signs in again once, instead of being
      quietly shadowed by someone else holding a valid token.
    * **Expired or revoked** — refused without touching the family.
    """
    token_hash = hash_secret(refresh_token, settings)
    statement = select(DeviceSession).where(DeviceSession.refresh_token_hash == token_hash)
    device_session = (await session.execute(statement)).scalar_one_or_none()

    if device_session is None:
        raise UnauthenticatedError("That refresh token is not valid")

    now = datetime.now(UTC)

    if device_session.revoked_reason == SessionRevocationReason.rotated:
        await _revoke_family(
            session,
            family_id=device_session.family_id,
            reason=SessionRevocationReason.reuse_detected,
            now=now,
        )
        logger.warning(
            "refresh_token_reuse_detected",
            extra={"user_id": str(device_session.user_id)},
        )
        await record_audit_event(
            session,
            action=AuditAction.refresh_token_reuse_detected,
            actor_id=device_session.user_id,
            target_type="session_family",
            target_id=device_session.family_id,
        )
        # Committed before raising, and this is the whole point of the branch.
        # The request session is a unit of work that rolls back on an exception,
        # so without this commit the revocation would be discarded by the very
        # error that reports it: the reused token would be refused while every
        # other token in the family kept working — which is precisely the
        # situation reuse detection exists to prevent. Verified by
        # `test_reusing_a_token_revokes_the_whole_family`.
        await session.commit()
        raise UnauthenticatedError(
            "This session has been ended for your security. Please sign in again."
        )

    if not device_session.is_usable(now):
        raise UnauthenticatedError("That session has expired. Please sign in again.")

    user = await session.get(User, device_session.user_id)
    if user is None or user.deleted_at is not None:
        raise UnauthenticatedError("That session is no longer valid")
    _assert_can_sign_in(user)

    device_session.revoked_at = now
    device_session.revoked_reason = SessionRevocationReason.rotated

    tokens = await issue_tokens(
        session,
        user=user,
        settings=settings,
        device_name=device_name or device_session.device_name,
        device_platform=device_platform or device_session.device_platform,
        family_id=device_session.family_id,
    )
    device_session.replaced_by_id = tokens.session_id
    await touch_last_active(session, user.id)
    return tokens


async def _revoke_family(
    session: AsyncSession,
    *,
    family_id: uuid.UUID,
    reason: SessionRevocationReason,
    now: datetime,
) -> int:
    statement = select(DeviceSession).where(
        DeviceSession.family_id == family_id, DeviceSession.revoked_at.is_(None)
    )
    revoked = 0
    for device_session in (await session.execute(statement)).scalars():
        device_session.revoked_at = now
        device_session.revoked_reason = reason
        revoked += 1
    return revoked


async def _evict_oldest_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, settings: Settings
) -> None:
    """Keep a user's live session count under the configured limit.

    Oldest first, so the device being signed in right now survives. Without this,
    every reinstall leaves a usable refresh token behind for thirty days.
    """
    live = (
        (
            await session.execute(
                select(DeviceSession)
                .where(
                    DeviceSession.user_id == user_id,
                    DeviceSession.revoked_at.is_(None),
                    DeviceSession.expires_at > datetime.now(UTC),
                )
                .order_by(DeviceSession.last_used_at.asc())
            )
        )
        .scalars()
        .all()
    )
    excess = len(live) - (settings.max_sessions_per_user - 1)
    if excess <= 0:
        return

    now = datetime.now(UTC)
    for device_session in live[:excess]:
        device_session.revoked_at = now
        device_session.revoked_reason = SessionRevocationReason.max_sessions


async def logout(session: AsyncSession, *, refresh_token: str, settings: Settings) -> None:
    """End one device's session.

    Succeeds silently for a token that is already gone: logging out is an
    outcome, not an operation, and failing a retry would only make clients
    paper over it.
    """
    token_hash = hash_secret(refresh_token, settings)
    statement = select(DeviceSession).where(DeviceSession.refresh_token_hash == token_hash)
    device_session = (await session.execute(statement)).scalar_one_or_none()
    if device_session is None or device_session.revoked_at is not None:
        return

    device_session.revoked_at = datetime.now(UTC)
    device_session.revoked_reason = SessionRevocationReason.logout


async def logout_all(
    session: AsyncSession, *, user: User, keep_session_id: uuid.UUID | None = None
) -> int:
    """Revoke every session, optionally sparing the one making the request.

    The spare exists so "sign out my other devices" does not also sign out the
    phone the user is holding.
    """
    statement = select(DeviceSession).where(
        DeviceSession.user_id == user.id, DeviceSession.revoked_at.is_(None)
    )
    now = datetime.now(UTC)
    revoked = 0
    for device_session in (await session.execute(statement)).scalars():
        if keep_session_id is not None and device_session.id == keep_session_id:
            continue
        device_session.revoked_at = now
        device_session.revoked_reason = SessionRevocationReason.logout_all
        revoked += 1

    if revoked:
        await record_audit_event(
            session,
            action=AuditAction.sessions_revoked,
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            context={"count": revoked, "kept_current": keep_session_id is not None},
        )
    return revoked


async def list_sessions(session: AsyncSession, *, user_id: uuid.UUID) -> list[DeviceSession]:
    """Live sessions, newest first, for the "where am I signed in?" screen."""
    statement = (
        select(DeviceSession)
        .where(
            DeviceSession.user_id == user_id,
            DeviceSession.revoked_at.is_(None),
            DeviceSession.expires_at > datetime.now(UTC),
        )
        .order_by(DeviceSession.last_used_at.desc())
    )
    return list((await session.execute(statement)).scalars().all())


async def get_session_for_access(
    session: AsyncSession, *, session_id: uuid.UUID
) -> DeviceSession | None:
    """Load a session named by an access token, to confirm it is still live.

    This is what makes "log out this device" take effect on access tokens as well
    as refresh tokens. It costs one indexed lookup per authenticated request,
    which is the price of logout meaning something before the access token's
    fifteen minutes are up.
    """
    return await session.get(DeviceSession, session_id)


async def revoke_sessions_for_deleted_account(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    statement = select(DeviceSession).where(
        DeviceSession.user_id == user_id, DeviceSession.revoked_at.is_(None)
    )
    now = datetime.now(UTC)
    for device_session in (await session.execute(statement)).scalars():
        device_session.revoked_at = now
        device_session.revoked_reason = SessionRevocationReason.account_deleted


async def count_live_sessions(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    statement = select(func.count(DeviceSession.id)).where(
        DeviceSession.user_id == user_id,
        DeviceSession.revoked_at.is_(None),
        DeviceSession.expires_at > datetime.now(UTC),
    )
    return (await session.execute(statement)).scalar_one()


async def raise_if_pending_deletion(user: User) -> None:
    """Deletion-pending accounts can still sign in, to cancel the request."""
    if user.status == UserStatus.deletion_pending and user.deletion_requested_at is None:
        raise ConflictError("Account state is inconsistent")
