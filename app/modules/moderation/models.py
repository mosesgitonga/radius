"""The audit log.

Phase 8 owns moderation proper — the queue, moderator actions, user history, the
admin console. This module grows into it.

Phase 1 brought the audit log. Phase 2 adds reports, the content lifecycle every
later module shares (`ContentStatus`), and the priority bands that let the Phase 8
queue order by harm rather than by arrival.

The audit log arrived first because the Data Protection Act requires an
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

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin, db_enum


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
    report_submitted = "report_submitted"
    content_auto_hidden = "content_auto_hidden"


_actor_type = db_enum(AuditActorType, name="audit_actor_type", length=20)
_action = db_enum(AuditAction, name="audit_action", length=48)


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

    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
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


class ContentStatus(StrEnum):
    """Whether a piece of user content is shown.

    Defined in the moderation module because moderation owns the decision; the
    feed and later modules import it rather than each inventing their own states.
    """

    visible = "visible"
    hidden_pending_review = "hidden_pending_review"
    """Auto-hidden after enough trusted reports, awaiting a human. Reversible, and
    the author can still see their own content — being invisible without being
    told is worse than being told."""
    removed = "removed"
    """Taken down by a moderator. Soft-deleted, because moderation needs the
    evidence and an appeal needs the content."""


class ReportReason(StrEnum):
    """Why something was reported.

    `job_scam` is separate from `scam` deliberately: the brief requires job scams
    to be prioritised, and that is only possible if they are distinguishable.
    """

    spam = "spam"
    scam = "scam"
    job_scam = "job_scam"
    harassment = "harassment"
    hate_speech = "hate_speech"
    violence = "violence"
    sexual_content = "sexual_content"
    prohibited_item = "prohibited_item"
    impersonation = "impersonation"
    misinformation = "misinformation"
    other = "other"


class ReportStatus(StrEnum):
    pending = "pending"
    upheld = "upheld"
    dismissed = "dismissed"


class ReportTargetType(StrEnum):
    post = "post"
    comment = "comment"
    story = "story"
    user = "user"
    listing = "listing"
    """Phase 5."""
    service_post = "service_post"
    """Phase 6."""
    event = "event"
    """Phase 7."""
    group = "group"
    """Phase 4."""


# Priority bands, so the Phase 8 queue can order by harm rather than by arrival.
# Safety and fraud first, as the brief requires.
REPORT_PRIORITY: dict[ReportReason, int] = {
    ReportReason.violence: 100,
    ReportReason.job_scam: 90,
    ReportReason.scam: 85,
    ReportReason.sexual_content: 80,
    ReportReason.hate_speech: 75,
    ReportReason.harassment: 70,
    ReportReason.impersonation: 50,
    ReportReason.prohibited_item: 40,
    ReportReason.misinformation: 30,
    ReportReason.spam: 20,
    ReportReason.other: 10,
}

_content_status = db_enum(ContentStatus, name="content_status", length=32)
_report_reason = db_enum(ReportReason, name="report_reason", length=32)
_report_status = db_enum(ReportStatus, name="report_status", length=20)
_report_target = db_enum(ReportTargetType, name="report_target_type", length=20)


class Report(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One report of one thing by one person.

    The target is a type and a bare UUID rather than a set of nullable foreign
    keys. Nine target types are already foreseen across the phases, and nine
    nullable columns with a check constraint that exactly one is set would be
    worse than this in every way that matters — including that a report should
    survive the hard deletion of whatever it was about.
    """

    __tablename__ = "reports"

    reporter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[ReportTargetType] = mapped_column(_report_target)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)

    reason: Mapped[ReportReason] = mapped_column(_report_reason)
    details: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    """Optional free text from the reporter. Capped, because a report is a signal
    rather than a correspondence."""

    status: Mapped[ReportStatus] = mapped_column(
        _report_status, default=ReportStatus.pending, server_default=ReportStatus.pending.value
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Denormalised from the reason at creation, so the queue can order by it
    without a CASE expression — and so the bands can be retuned without
    rewriting history."""

    counted_towards_auto_hide: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    """Whether the reporter was trusted at the time. Recorded rather than
    recomputed, because trust changes and the decision to hide should remain
    explicable afterwards."""

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    """A moderator, from Phase 8. Not a foreign key for the same reason the audit
    log's actor is not: the record outlives accounts."""

    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    __table_args__ = (
        # One report per person per thing. A second report from the same user adds
        # no information and would let one person reach the auto-hide threshold
        # alone, which is exactly what the threshold exists to prevent.
        UniqueConstraint(
            "reporter_id",
            "target_type",
            "target_id",
            name="uq_reports_reporter_id_target_type_target_id",
        ),
        # Counting distinct trusted reports for one target.
        Index("ix_reports_target_type_target_id", "target_type", "target_id"),
        # The Phase 8 queue: pending work, worst first, oldest first.
        Index("ix_reports_status_priority_created_at", "status", "priority", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Report {self.reason} on {self.target_type}:{self.target_id}>"
