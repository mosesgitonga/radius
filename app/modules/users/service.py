"""User service: profiles, consent, blocking, and data rights.

Two functions here are load-bearing well beyond this module.

`blocked_user_ids` is the single implementation of the blocking rule. Every module
from Phase 2 onwards — feed, messaging, marketplace, services, events — filters
through it. It is written once because a blocking rule with two implementations
has one that is wrong, and the wrong one is how a blocked person's content reaches
somebody who asked never to see it again.

`export_user_data` is the Data Protection Act right of access. It grows with every
phase, and the rule recorded in `docs/DATA_MAP.md` is that a phase adding personal
data extends this function in the same change. A field nobody exported is a field
nobody can erase either.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.modules.moderation.models import AuditAction, AuditActorType
from app.modules.moderation.service import record_audit_event
from app.modules.users.models import (
    Block,
    Consent,
    ConsentType,
    User,
    UserStatus,
)

logger = logging.getLogger(__name__)

# Consents a new account must actively grant. Marketing is deliberately absent:
# it is optional, and bundling it into signup would make it not a choice.
REQUIRED_CONSENTS: tuple[ConsentType, ...] = (
    ConsentType.terms,
    ConsentType.privacy_policy,
    ConsentType.location_use,
)

OPTIONAL_CONSENTS: tuple[ConsentType, ...] = (ConsentType.marketing,)


@dataclass(frozen=True, slots=True)
class DeletionStatus:
    requested_at: datetime
    purge_after: datetime


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """A user by ID, excluding soft-deleted rows."""
    statement = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    return (await session.execute(statement)).scalar_one_or_none()


async def require_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await get_user(session, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return user


async def get_user_by_phone(session: AsyncSession, phone_number: str) -> User | None:
    statement = select(User).where(User.phone_number == phone_number, User.deleted_at.is_(None))
    return (await session.execute(statement)).scalar_one_or_none()


async def touch_last_active(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Record that the app was opened, which is what "active" means for density.

    A bare UPDATE rather than loading the row and mutating it: this runs on
    authenticated requests, and it must not depend on, or conflict with, whatever
    else the request is doing to that user.
    """
    await session.execute(
        update(User).where(User.id == user_id).values(last_active_at=datetime.now(UTC))
    )


# ---------------------------------------------------------------------------
# Account creation and profile
# ---------------------------------------------------------------------------
async def create_user(
    session: AsyncSession,
    *,
    phone_number: str,
    display_name: str,
    settings: Settings,
    granted_consents: set[ConsentType],
) -> User:
    """Create an account, with its consent records, in one transaction.

    `adult_confirmed_at` is a timestamp, not a flag: the Act asks when a
    declaration was made, and a bare `true` records nothing worth having.
    """
    missing = set(REQUIRED_CONSENTS) - granted_consents
    if missing:
        raise BadRequestError(
            "Required consents are missing",
            details={"missing": sorted(str(consent) for consent in missing)},
        )

    now = datetime.now(UTC)
    user = User(
        phone_number=phone_number,
        display_name=display_name.strip(),
        phone_verified_at=now,
        adult_confirmed_at=now,
        last_active_at=now,
        status=UserStatus.active,
    )
    session.add(user)
    await session.flush()

    for consent_type in granted_consents:
        _add_consent(session, user, consent_type, granted=True, settings=settings)

    await record_audit_event(
        session,
        action=AuditAction.account_created,
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        context={"consents": sorted(str(c) for c in granted_consents)},
    )
    return user


async def update_profile(session: AsyncSession, *, user: User, display_name: str | None) -> User:
    changed: dict[str, Any] = {}
    if display_name is not None:
        cleaned = display_name.strip()
        if not cleaned:
            raise BadRequestError("Display name cannot be blank")
        if cleaned != user.display_name:
            changed["display_name"] = True
            user.display_name = cleaned

    if changed:
        await record_audit_event(
            session,
            action=AuditAction.profile_updated,
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            # Which fields changed, not what they changed to: a display name is
            # personal data and the audit log is kept for two years.
            context={"fields": sorted(changed)},
        )
    return user


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
def _add_consent(
    session: AsyncSession,
    user: User,
    consent_type: ConsentType,
    *,
    granted: bool,
    settings: Settings,
) -> Consent:
    consent = Consent(
        user_id=user.id,
        consent_type=consent_type,
        granted=granted,
        document_version=_document_version(consent_type, settings),
    )
    session.add(consent)
    return consent


def _document_version(consent_type: ConsentType, settings: Settings) -> str:
    """Which document version a decision relates to.

    Location and marketing consent are not documents of their own; they are
    granted under the privacy policy, so they carry its version. That is what
    makes it possible later to say who agreed under which policy.
    """
    if consent_type == ConsentType.terms:
        return settings.terms_version
    return settings.privacy_policy_version


async def set_consent(
    session: AsyncSession,
    *,
    user: User,
    consent_type: ConsentType,
    granted: bool,
    settings: Settings,
) -> Consent:
    """Record a consent decision.

    Required consents cannot be withdrawn while the account exists — the features
    they cover are the product. Withdrawing them means deleting the account, and
    saying so plainly is better than offering a toggle that silently breaks
    everything.
    """
    if not granted and consent_type in REQUIRED_CONSENTS:
        raise ConflictError(
            "This consent cannot be withdrawn without deleting the account",
            details={
                "consent_type": str(consent_type),
                "alternative": "request account deletion",
            },
        )

    consent = _add_consent(session, user, consent_type, granted=granted, settings=settings)
    await record_audit_event(
        session,
        action=AuditAction.consent_granted if granted else AuditAction.consent_withdrawn,
        actor_id=user.id,
        target_type="consent",
        target_id=user.id,
        context={"consent_type": str(consent_type), "version": consent.document_version},
    )
    return consent


async def current_consents(session: AsyncSession, user_id: uuid.UUID) -> dict[ConsentType, Consent]:
    """The newest decision per consent type.

    The table is append-only, so current state is the latest row in each group.
    Ordered oldest-first and overwritten as it goes, which is cheaper than a
    window function over what will never be more than a handful of rows per user.
    """
    statement = (
        select(Consent)
        .where(Consent.user_id == user_id)
        .order_by(Consent.consent_type, Consent.created_at.asc())
    )
    newest: dict[ConsentType, Consent] = {}
    for consent in (await session.execute(statement)).scalars():
        newest[consent.consent_type] = consent
    return newest


# ---------------------------------------------------------------------------
# Blocking — the shared rule
# ---------------------------------------------------------------------------
async def blocked_user_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Every user who must be invisible to this one, in both directions.

    **This is the function every other module must use.** Blocking is recorded
    one-way — A blocks B, so only A can unblock — but it takes effect both ways:
    if A blocked B, then B must not see A either. Returning only the people A
    blocked would let B keep watching A's posts and messaging them, which is
    exactly what A was trying to stop.
    """
    statement = select(Block.blocker_id, Block.blocked_id).where(
        or_(Block.blocker_id == user_id, Block.blocked_id == user_id)
    )
    rows = (await session.execute(statement)).all()
    return {
        other
        for blocker_id, blocked_id in rows
        for other in (blocker_id, blocked_id)
        if other != user_id
    }


async def is_blocked_between(session: AsyncSession, first: uuid.UUID, second: uuid.UUID) -> bool:
    """Whether either of two users has blocked the other.

    For the single-pair case, so callers do not fetch a whole block list to answer
    one question.
    """
    statement = (
        select(Block.id)
        .where(
            or_(
                (Block.blocker_id == first) & (Block.blocked_id == second),
                (Block.blocker_id == second) & (Block.blocked_id == first),
            )
        )
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def block_user(session: AsyncSession, *, blocker: User, blocked_id: uuid.UUID) -> Block:
    if blocker.id == blocked_id:
        raise BadRequestError("You cannot block yourself")

    target = await get_user(session, blocked_id)
    if target is None:
        raise NotFoundError("User not found")

    existing = await _find_block(session, blocker.id, blocked_id)
    if existing is not None:
        # Idempotent: a client retrying on a flaky connection should not get an
        # error for an outcome that already holds.
        return existing

    block = Block(blocker_id=blocker.id, blocked_id=blocked_id)
    session.add(block)
    await record_audit_event(
        session,
        action=AuditAction.user_blocked,
        actor_id=blocker.id,
        target_type="user",
        target_id=blocked_id,
    )
    return block


async def unblock_user(session: AsyncSession, *, blocker: User, blocked_id: uuid.UUID) -> None:
    block = await _find_block(session, blocker.id, blocked_id)
    if block is None:
        raise NotFoundError("That user is not blocked")
    await session.delete(block)
    await record_audit_event(
        session,
        action=AuditAction.user_unblocked,
        actor_id=blocker.id,
        target_type="user",
        target_id=blocked_id,
    )


async def _find_block(
    session: AsyncSession, blocker_id: uuid.UUID, blocked_id: uuid.UUID
) -> Block | None:
    statement = select(Block).where(Block.blocker_id == blocker_id, Block.blocked_id == blocked_id)
    return (await session.execute(statement)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def request_deletion(
    session: AsyncSession, *, user: User, settings: Settings
) -> DeletionStatus:
    """Start the deletion grace period.

    Reversible for the configured window, then purged by a worker. The grace
    period exists because account deletion is irreversible and frequently
    regretted — or coerced.
    """
    if user.deletion_requested_at is not None:
        raise ConflictError(
            "Deletion has already been requested",
            details={"requested_at": user.deletion_requested_at.isoformat()},
        )

    now = datetime.now(UTC)
    user.deletion_requested_at = now
    user.status = UserStatus.deletion_pending

    await record_audit_event(
        session,
        action=AuditAction.account_deletion_requested,
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        context={"grace_period_days": settings.deletion_grace_period_days},
    )
    return DeletionStatus(
        requested_at=now,
        purge_after=now + timedelta(days=settings.deletion_grace_period_days),
    )


async def cancel_deletion(session: AsyncSession, *, user: User) -> None:
    if user.deletion_requested_at is None:
        raise ConflictError("No deletion request is pending")

    user.deletion_requested_at = None
    user.status = UserStatus.active
    await record_audit_event(
        session,
        action=AuditAction.account_deletion_cancelled,
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
    )


async def export_user_data(
    session: AsyncSession, *, user: User, settings: Settings
) -> dict[str, Any]:
    """Everything held about a user, as JSON. The Act's right of access.

    Two absences are deliberate and are stated in the export itself, because "we
    hold nothing" is a more useful answer than silence:

    * There is no location history. Only the current home area exists, and it is
      exported as its label — not as a cell or a coordinate, since handing those
      back would recreate the precision the system exists to avoid.
    * There is no explore history. Those locations live in Redis for two hours and
      are never written down.

    Extended by every later phase. See `docs/DATA_MAP.md`.
    """
    consents = await current_consents(session, user.id)
    blocks = (
        await session.execute(
            select(Block.blocked_id, Block.created_at).where(Block.blocker_id == user.id)
        )
    ).all()

    await record_audit_event(
        session,
        action=AuditAction.data_exported,
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
    )

    return {
        "export_generated_at": datetime.now(UTC).isoformat(),
        "export_format_version": 1,
        "account": {
            "id": str(user.id),
            "phone_number": user.phone_number,
            "display_name": user.display_name,
            "status": str(user.status),
            "member_since": user.created_at.isoformat() if user.created_at else None,
            "phone_verified_at": (
                user.phone_verified_at.isoformat() if user.phone_verified_at else None
            ),
            "adult_confirmed_at": user.adult_confirmed_at.isoformat(),
            "last_active_at": user.last_active_at.isoformat(),
            "deletion_requested_at": (
                user.deletion_requested_at.isoformat() if user.deletion_requested_at else None
            ),
        },
        "consents": [
            {
                "type": str(consent_type),
                "granted": consent.granted,
                "document_version": consent.document_version,
                "recorded_at": consent.created_at.isoformat(),
            }
            for consent_type, consent in sorted(consents.items())
        ],
        "blocks": [
            {"blocked_user_id": str(blocked_id), "blocked_at": created_at.isoformat()}
            for blocked_id, created_at in blocks
        ],
        "location": {
            "home_area_set": user.has_home_location,
            "home_set_at": user.home_set_at.isoformat() if user.home_set_at else None,
            "notes": (
                "Your home area is stored only as an approximate H3 cell and its "
                "centre point, never as the coordinates your phone sent. No "
                "location history is kept. Explore-mode locations are held "
                "temporarily in memory and are never written to the database."
            ),
        },
        "verified_neighbour": {
            "is_verified": user.is_verified_neighbour,
            "verified_at": (
                user.verified_neighbour_at.isoformat() if user.verified_neighbour_at else None
            ),
            "home_presence_days_recorded": bin(user.home_presence_mask).count("1"),
            "notes": (
                "The verified-neighbour check records only one yes/no per day for "
                "whether you were in your home area, never where you actually were."
            ),
        },
        "not_held": [
            "location history",
            "explore-mode locations",
            "raw GPS coordinates",
            "message content (no messaging yet)",
        ],
        "retention_policy_days": {
            "deletion_grace_period": settings.deletion_grace_period_days,
            "application_logs": 30,
        },
    }


async def purge_user(session: AsyncSession, *, user: User) -> None:
    """Irreversibly purge an account whose grace period has run out.

    Personal fields are overwritten rather than the row deleted, because other
    tables reference it and a cascade would take moderation evidence with it. The
    phone number is replaced with a value that keeps the unique index satisfied
    while being unusable for sign-in.

    The audit trail is untouched on purpose: a log that disappears with the
    account is useless for the investigations it exists for.
    """
    user.phone_number = f"purged:{user.id}"
    user.display_name = "Deleted user"
    user.phone_verified_at = None
    user.avatar_media_id = None
    user.home_cell_res9 = None
    user.home_cell_res8 = None
    user.home_cell_res7 = None
    user.home_cell_res6 = None
    user.home_centroid = None
    user.home_area_id = None
    user.home_set_at = None
    user.home_changed_at = None
    user.home_presence_mask = 0
    user.home_presence_updated_on = None
    user.is_verified_neighbour = False
    user.verified_neighbour_at = None
    user.status = UserStatus.banned
    user.deleted_at = datetime.now(UTC)

    await record_audit_event(
        session,
        action=AuditAction.account_purged,
        actor_type=AuditActorType.system,
        actor_id=None,
        target_type="user",
        target_id=user.id,
    )
    logger.info("account_purged", extra={"user_id": str(user.id)})
