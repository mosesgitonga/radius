"""Request and response models for reporting."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.moderation.models import ReportReason, ReportStatus, ReportTargetType


class ReportIn(BaseModel):
    target_type: ReportTargetType = Field(examples=["post"])
    target_id: uuid.UUID
    reason: ReportReason = Field(examples=["scam"])
    details: str | None = Field(default=None, max_length=1000)


class ReportOut(BaseModel):
    """Acknowledgement of a report.

    Deliberately says nothing about what happened to the content. Telling a
    reporter "that was hidden" would let anyone measure how many reports it takes,
    and a reporter learning that their report tipped something over is not their
    business.
    """

    id: uuid.UUID
    target_type: ReportTargetType
    target_id: uuid.UUID
    reason: ReportReason
    status: ReportStatus
    created_at: datetime
