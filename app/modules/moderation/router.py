"""Reporting endpoint.

The moderator side — the queue, actions, user history — is Phase 8 and will live
behind a separate admin authentication. This is only the neighbour's side.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.modules.auth.dependencies import CurrentUser, user_rate_limit
from app.modules.moderation import service as moderation_service
from app.modules.moderation.schemas import ReportIn, ReportOut

router = APIRouter(tags=["moderation"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post(
    "/reports",
    response_model=ReportOut,
    status_code=status.HTTP_201_CREATED,
    summary="Report a post, comment, story, or neighbour",
    description=(
        "Files a report for review. Content reported by enough distinct, established "
        "neighbours is hidden until a moderator looks at it.\n\n"
        "One report per person per thing: reporting again returns the original "
        "report and does not count twice. The response never says whether anything "
        "was hidden — that would let anyone measure how many reports it takes."
    ),
    dependencies=[
        # Reporting is the tool for stopping abuse, so it must stay usable — but a
        # report flood is itself abuse of the moderation queue.
        Depends(user_rate_limit(bucket="report", limit=30, window_seconds=86_400))
    ],
)
async def submit_report(
    payload: ReportIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ReportOut:
    outcome = await moderation_service.submit_report(
        session,
        reporter=user,
        target_type=payload.target_type,
        target_id=payload.target_id,
        reason=payload.reason,
        details=payload.details,
        settings=settings,
    )
    await session.flush()
    report = outcome.report
    return ReportOut(
        id=report.id,
        target_type=report.target_type,
        target_id=report.target_id,
        reason=report.reason,
        status=report.status,
        created_at=report.created_at,
    )
