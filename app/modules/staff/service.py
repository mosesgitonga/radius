"""Staff accounts and sign-in.

Sign-in takes all three factors in one request — email, password, authenticator
code — and answers every failure with the same message, so a caller learns
nothing about which part was wrong or whether the email exists. Consecutive
failures lock the account for a while; the lock is answered with that same
message too, so locking a moderator out reveals nothing either.

Every sign-in, failed or not, is audited with actor type `moderator`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import execute_dml
from app.core.errors import BadRequestError, ConflictError, NotFoundError, UnauthenticatedError
from app.modules.moderation.audit import record_audit_event
from app.modules.moderation.models import AuditAction, AuditActorType
from app.modules.staff import credentials
from app.modules.staff.models import StaffAccount, StaffRole, StaffSession

logger = logging.getLogger(__name__)

SIGN_IN_FAILED = "Those details are not right"


@dataclass(frozen=True, slots=True)
class IssuedCredentials:
    """Shown once, when an account is created or its credentials reset."""

    account: StaffAccount
    password: str
    totp_secret: str
    totp_uri: str


@dataclass(frozen=True, slots=True)
class SignedIn:
    account: StaffAccount
    session: StaffSession
    token: str


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def get_staff(session: AsyncSession, staff_id: uuid.UUID) -> StaffAccount | None:
    return await session.get(StaffAccount, staff_id)


async def find_by_email(session: AsyncSession, email: str) -> StaffAccount | None:
    return (
        await session.execute(
            select(StaffAccount).where(StaffAccount.email == normalise_email(email))
        )
    ).scalar_one_or_none()


def _issue(account: StaffAccount, settings: Settings) -> tuple[str, str, str]:
    password = credentials.generate_password()
    secret = credentials.generate_totp_secret()
    account.password_hash = credentials.hash_password(password)
    account.totp_secret = secret
    account.totp_last_step = None
    uri = credentials.provisioning_uri(
        secret=secret, email=account.email, issuer=f"{settings.app_name} staff"
    )
    return password, secret, uri


async def create_account(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    role: StaffRole,
    created_by: StaffAccount | None,
    settings: Settings,
) -> IssuedCredentials:
    """Create a staff account with a generated password and authenticator secret.

    Both are generated rather than chosen by the admin, so no admin ever knows a
    colleague's working password: the new moderator signs in with the temporary
    one and changes it.
    """
    email = normalise_email(email)
    if "@" not in email:
        raise BadRequestError("That is not an email address")
    account = StaffAccount(
        email=email,
        display_name=display_name.strip(),
        role=role,
        created_by_id=created_by.id if created_by else None,
        password_hash="",
        totp_secret="",
    )
    password, secret, uri = _issue(account, settings)
    try:
        async with session.begin_nested():
            session.add(account)
    except IntegrityError as exc:
        raise ConflictError("A staff account with that email already exists") from exc
    await record_audit_event(
        session,
        action=AuditAction.staff_account_created,
        actor_type=AuditActorType.moderator if created_by else AuditActorType.system,
        actor_id=created_by.id if created_by else None,
        target_type="staff",
        target_id=account.id,
        context={"role": str(role)},
    )
    return IssuedCredentials(account=account, password=password, totp_secret=secret, totp_uri=uri)


async def reset_credentials(
    session: AsyncSession, *, account: StaffAccount, by: StaffAccount, settings: Settings
) -> IssuedCredentials:
    """New password and authenticator secret — for a lost phone or a leak. Ends
    every session the account has."""
    password, secret, uri = _issue(account, settings)
    account.failed_logins = 0
    account.locked_until = None
    await revoke_sessions(session, account.id)
    await record_audit_event(
        session,
        action=AuditAction.staff_account_updated,
        actor_type=AuditActorType.moderator,
        actor_id=by.id,
        target_type="staff",
        target_id=account.id,
        context={"credentials_reset": True},
    )
    return IssuedCredentials(account=account, password=password, totp_secret=secret, totp_uri=uri)


async def _active_admin_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(StaffAccount.id)).where(
                StaffAccount.role == StaffRole.admin, StaffAccount.disabled_at.is_(None)
            )
        )
        or 0
    )


async def update_account(
    session: AsyncSession,
    *,
    account: StaffAccount,
    by: StaffAccount,
    role: StaffRole | None,
    disabled: bool | None,
    display_name: str | None,
) -> StaffAccount:
    """Change a role, a name, or whether the account may sign in.

    The last active admin cannot be demoted or disabled, and nobody can disable
    themselves: either would leave the console with no one able to manage it.
    """
    changes: dict[str, object] = {}
    losing_admin = account.role == StaffRole.admin and account.disabled_at is None
    if role is not None and role != account.role:
        if losing_admin and role != StaffRole.admin and await _active_admin_count(session) <= 1:
            raise BadRequestError("The last admin cannot be demoted")
        account.role = role
        changes["role"] = str(role)
    if disabled is not None and disabled != (account.disabled_at is not None):
        if disabled:
            if account.id == by.id:
                raise BadRequestError("You cannot disable your own account")
            if losing_admin and await _active_admin_count(session) <= 1:
                raise BadRequestError("The last admin cannot be disabled")
            account.disabled_at = datetime.now(UTC)
            await revoke_sessions(session, account.id)
        else:
            account.disabled_at = None
        changes["disabled"] = disabled
    if display_name is not None and display_name.strip() != account.display_name:
        account.display_name = display_name.strip()
        changes["display_name"] = True
    if changes:
        await record_audit_event(
            session,
            action=AuditAction.staff_account_updated,
            actor_type=AuditActorType.moderator,
            actor_id=by.id,
            target_type="staff",
            target_id=account.id,
            context=changes,
        )
    return account


async def change_password(
    session: AsyncSession,
    *,
    account: StaffAccount,
    current_password: str,
    new_password: str,
    code: str,
    settings: Settings,
) -> None:
    """Needs the current password and a fresh code, so a stolen staff token alone
    cannot take the account over."""
    if len(new_password) < settings.staff_password_min_length:
        raise BadRequestError(
            f"Use at least {settings.staff_password_min_length} characters",
            details={"min_length": settings.staff_password_min_length},
        )
    if not credentials.verify_password(current_password, account.password_hash):
        raise BadRequestError(SIGN_IN_FAILED)
    step = credentials.verify_totp(account.totp_secret, code, last_step=account.totp_last_step)
    if step is None:
        raise BadRequestError(SIGN_IN_FAILED)
    account.totp_last_step = step
    account.password_hash = credentials.hash_password(new_password)
    await record_audit_event(
        session,
        action=AuditAction.staff_account_updated,
        actor_type=AuditActorType.moderator,
        actor_id=account.id,
        target_type="staff",
        target_id=account.id,
        context={"password_changed": True},
    )


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
async def sign_in(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    code: str,
    ip_address: str | None,
    settings: Settings,
) -> SignedIn:
    """Check all three factors and open a staff session, or `UnauthenticatedError`.

    A failure must be committed, not rolled back — the counter and the audit row
    are the point — so the router commits before letting the error through.
    """
    now = datetime.now(UTC)
    account = await find_by_email(session, email)
    if account is None:
        credentials.burn_password_check(password)
        await _audit_failure(session, None, "unknown")
        raise UnauthenticatedError(SIGN_IN_FAILED)

    if account.disabled_at is not None:
        credentials.burn_password_check(password)
        await _audit_failure(session, account, "disabled")
        raise UnauthenticatedError(SIGN_IN_FAILED)
    if account.locked_until is not None and account.locked_until > now:
        credentials.burn_password_check(password)
        await _audit_failure(session, account, "locked")
        raise UnauthenticatedError(SIGN_IN_FAILED)

    password_ok = credentials.verify_password(password, account.password_hash)
    step = (
        credentials.verify_totp(account.totp_secret, code, last_step=account.totp_last_step)
        if password_ok
        else None
    )
    if step is None:
        account.failed_logins += 1
        locked = account.failed_logins >= settings.staff_max_failed_logins
        if locked:
            account.locked_until = now + timedelta(minutes=settings.staff_lockout_minutes)
            account.failed_logins = 0
        await _audit_failure(
            session, account, "password" if not password_ok else "code", locked=locked
        )
        raise UnauthenticatedError(SIGN_IN_FAILED)

    account.totp_last_step = step
    account.failed_logins = 0
    account.locked_until = None
    account.last_login_at = now
    staff_session = StaffSession(
        staff_id=account.id,
        expires_at=now + settings.staff_session_ttl,
        ip_address=ip_address,
    )
    session.add(staff_session)
    await session.flush()
    await record_audit_event(
        session,
        action=AuditAction.staff_signed_in,
        actor_type=AuditActorType.moderator,
        actor_id=account.id,
        target_type="staff_session",
        target_id=staff_session.id,
    )
    token = credentials.create_staff_token(
        staff_id=account.id,
        session_id=staff_session.id,
        expires_at=staff_session.expires_at,
        settings=settings,
    )
    return SignedIn(account=account, session=staff_session, token=token)


async def _audit_failure(
    session: AsyncSession, account: StaffAccount | None, why: str, *, locked: bool = False
) -> None:
    await record_audit_event(
        session,
        action=AuditAction.staff_sign_in_failed,
        actor_type=AuditActorType.moderator,
        actor_id=account.id if account else None,
        target_type="staff" if account else None,
        target_id=account.id if account else None,
        context={"failure": why, "locked": locked},
    )
    logger.warning("staff_sign_in_failed", extra={"failure": why, "locked": locked})


async def sign_out(session: AsyncSession, staff_session: StaffSession) -> None:
    staff_session.revoked_at = datetime.now(UTC)


async def revoke_sessions(session: AsyncSession, staff_id: uuid.UUID) -> None:
    await execute_dml(
        session,
        update(StaffSession)
        .where(StaffSession.staff_id == staff_id, StaffSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC)),
    )


async def authenticate(
    session: AsyncSession, *, token: str, settings: Settings
) -> tuple[StaffAccount, StaffSession]:
    """The staff member a token names, if the token, its session and the account
    are all still good."""
    claims = credentials.decode_staff_token(token, settings)
    staff_session = await session.get(StaffSession, claims.session_id)
    now = datetime.now(UTC)
    if (
        staff_session is None
        or staff_session.staff_id != claims.staff_id
        or staff_session.revoked_at is not None
        or staff_session.expires_at <= now
    ):
        raise UnauthenticatedError("That staff session is no longer valid")
    account = await session.get(StaffAccount, claims.staff_id)
    if account is None or account.disabled_at is not None:
        raise UnauthenticatedError("That staff session is no longer valid")
    return account, staff_session


async def list_accounts(session: AsyncSession) -> list[StaffAccount]:
    return list(
        (await session.execute(select(StaffAccount).order_by(StaffAccount.created_at))).scalars()
    )


async def require_account(session: AsyncSession, staff_id: uuid.UUID) -> StaffAccount:
    account = await get_staff(session, staff_id)
    if account is None:
        raise NotFoundError("Staff account not found")
    return account
