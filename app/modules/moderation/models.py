"""The audit log.

Phase 8 owns moderation proper — the report queue, moderator actions, user
history. This table arrives in Phase 1 because the Data Protection Act requires an
audit trail "ready for breach investigation" from the moment there is personal
data to breach, and because data-rights actions are exactly what has to be
auditable: who asked for an export, who requested deletion, who withdrew consent,
and when.

Every module writes here through `moderation.service.record_audit_event`, so there
is one implementation and one place to review.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin


class AuditActorType(StrEnum):
    user = "user"
    """The data subject acted on their own account."""
    moderator = "moderator"
    """Phase 8. Separate from `user` because moderator powers are separate."""
    system = "system"
    """A worker: a retention purge, a badge recomputation, an expiry sweep."""


class AuditAction(StrEnum):
    """Audited actions. Extended by each phase that adds something worth auditing.

    Only consequential events belong here. An audit log that records every read
    becomes an expensive thing nobody searches.
    """

    # Account lifecycle
    account_created = "account_created"
    account_deletion_requested = "account_deletion_requested"
    account_deletion_cancelled = "account_deletion_cancelled"
    account_purged = "account_purged"

    # Data rights
    data_exported = "data_exported"
    consent_granted = "consent_granted"
    consent_withdrawn = "consent_withdrawn"
    profile_updated = "profile_updated"

    # Authentication
    sessions_revoked = "sessions_revoked"
    refresh_token_reuse_detected = "refresh_token_reuse_detected"  # noqa: S105 — an action name
    """Security-relevant: either a stolen token or a client bug, and the
    difference matters."""

    # Location
    home_location_set = "home_location_set"
    home_location_changed = "home_location_changed"
    location_spoof_suspected = "location_spoof_suspected"

    # Safety
    user_blocked = "user_blocked"
    user_unblocked = "user_unblocked"


_actor_type = Enum(AuditActorType, name="audit_actor_type", native_enum=False, length=20)
_action = Enum(AuditAction, name="audit_action", native_enum=False, length=48)


class AuditEvent(Base, UUIDPrimaryKeyMixin):
    """One audited action. Append-only: nothing updates or deletes these rows.

    Not soft-deletable and not purged with the account it concerns. An audit log
    that disappears when the account does is useless for exactly the
    investigations it exists for, so `actor_id` is a bare UUID rather than a
    foreign key — the row outlives the user.
    """

    __tablename__ = "audit_events"

    actor_type: Mapped[AuditActorType] = mapped_column(_actor_type)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    """Deliberately not a foreign key: the row must survive the account's purge.
    Null for system actors."""

    action: Mapped[AuditAction] = mapped_column(_action, index=True)

    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)

    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    """Structured detail: the old and new values of a changed field, the session
    family that was revoked, the implied speed of a suspected spoof.

    **Never coordinates, cell IDs, phone numbers, or tokens.** The audit log is
    read by humans during incidents and is kept for two years, which makes it the
    worst possible place for the data the rest of the system works to avoid
    storing. `record_audit_event` enforces this rather than trusting callers."""

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Ties the row to the request that caused it, and so to the application logs."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        # "What happened to this account?" and "who did this action lately?"
        Index("ix_audit_events_actor_id_created_at", "actor_id", "created_at"),
        Index("ix_audit_events_action_created_at", "action", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<AuditEvent {self.action} actor={self.actor_id}>"
