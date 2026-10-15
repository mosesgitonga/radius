"""Request and response models for the moderation console.

What a moderator sees of a person is what the job needs: name, standing and
history. Never a phone number, a location, or a cell.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.modules.media.schemas import ImageVariantOut
from app.modules.moderation.models import (
    AuditActorType,
    ReportReason,
    ReportStatus,
    ReportTargetType,
)
from app.modules.users.models import User, UserStatus
from app.modules.users.standing import is_restricted


class AccountSummaryOut(BaseModel):
    id: uuid.UUID
    display_name: str
    status: UserStatus
    created_at: datetime
    is_verified_neighbour: bool
    badge_locked: bool
    restricted: bool
    restricted_until: datetime | None
    restriction_reason: str | None
    suspended_until: datetime | None
    deleted: bool

    @classmethod
    def of(cls, user: User) -> AccountSummaryOut:
        return cls(
            id=user.id,
            display_name=user.display_name,
            status=user.status,
            created_at=user.created_at,
            is_verified_neighbour=user.is_verified_neighbour,
            badge_locked=user.badge_locked_at is not None,
            restricted=is_restricted(user),
            restricted_until=user.restricted_until,
            restriction_reason=user.restriction_reason,
            suspended_until=user.suspended_until,
            deleted=user.deleted_at is not None,
        )


class QueueItemOut(BaseModel):
    target_type: ReportTargetType
    target_id: uuid.UUID
    priority: int
    waiting_since: datetime
    report_count: int
    trusted_report_count: int
    reasons: list[ReportReason] = Field(description="Worst first.")
    held: bool = Field(description="Hidden or held, waiting for a moderator.")
    excerpt: str
    author: AccountSummaryOut | None


class AdminReportOut(BaseModel):
    id: uuid.UUID
    reporter_id: uuid.UUID
    reason: ReportReason
    details: str | None
    trusted: bool
    status: ReportStatus
    created_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None


class ContentImageOut(BaseModel):
    media_id: uuid.UUID
    variants: dict[str, ImageVariantOut]


class ContentOut(BaseModel):
    target_type: ReportTargetType
    target_id: uuid.UUID
    status: str = Field(description="`visible`, `hidden_pending_review`, `removed` or `deleted`.")
    text: str
    created_at: datetime | None
    images: list[ContentImageOut]
    details: dict[str, Any]
    author: AccountSummaryOut | None


class ContentDetailOut(BaseModel):
    content: ContentOut
    reports: list[AdminReportOut]


class RemoveIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class NoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ResolveIn(BaseModel):
    outcome: Literal["upheld", "dismissed"]
    note: str | None = Field(default=None, max_length=1000)


class ActionOut(BaseModel):
    reports_resolved: int


class WarnIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000, description="Shown to the user.")
    report_id: uuid.UUID | None = None


class SuspendIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    days: int | None = Field(
        default=None, ge=1, le=365, description="Omit to suspend until lifted by hand."
    )


class RestrictIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    days: int = Field(default=7, ge=1, le=90)


class BanIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    ban_phone: bool = True
    ban_devices: bool = True
    remove_content: bool = Field(
        default=False, description="Also take down everything they have posted."
    )


class BadgeRevokeIn(BaseModel):
    lock: bool = Field(
        default=True, description="Keep the daily recomputation from awarding it back."
    )


class WarningOut(BaseModel):
    id: uuid.UUID
    reason: str
    report_id: uuid.UUID | None
    staff_id: uuid.UUID | None
    created_at: datetime


class AuditEventOut(BaseModel):
    id: uuid.UUID
    actor_type: AuditActorType
    actor_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: uuid.UUID | None
    context: dict[str, Any]
    request_id: str | None
    created_at: datetime


class UserHistoryOut(BaseModel):
    account: AccountSummaryOut
    reports_against: list[AdminReportOut]
    reports_by: list[AdminReportOut]
    warnings: list[WarningOut]
    actions: list[AuditEventOut]
    content_removed_count: int


class PhoneBanIn(BaseModel):
    phone_number: str = Field(max_length=32, examples=["+254712345678"])
    reason: str | None = Field(default=None, max_length=1000)


class BanEntryOut(BaseModel):
    """A ban-list entry. The number or device itself is never shown: only its
    keyed hash is stored."""

    id: uuid.UUID
    user_id: uuid.UUID | None
    reason: str | None
    staff_id: uuid.UUID | None
    created_at: datetime
