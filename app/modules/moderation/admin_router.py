"""The moderation console, under `/admin`. Staff tokens only.

Moderators work the queue and act on content and accounts. Bans, the ban lists
and the audit log are for admins.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.pagination import CursorPage, PageParams, page_params, paginate
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.modules.auth.phone import normalise_phone_number
from app.modules.feed.views import load_assets
from app.modules.media.views import image_variants
from app.modules.moderation import console
from app.modules.moderation.admin_schemas import (
    AccountSummaryOut,
    ActionOut,
    AdminReportOut,
    AuditEventOut,
    BadgeRevokeIn,
    BanEntryOut,
    BanIn,
    ContentDetailOut,
    ContentImageOut,
    ContentOut,
    NoteIn,
    PhoneBanIn,
    QueueItemOut,
    RemoveIn,
    ResolveIn,
    RestrictIn,
    SuspendIn,
    UserHistoryOut,
    WarnIn,
    WarningOut,
)
from app.modules.moderation.models import (
    AuditAction,
    AuditActorType,
    AuditEvent,
    BannedDevice,
    BannedPhone,
    ModerationWarning,
    Report,
    ReportReason,
    ReportStatus,
    ReportTargetType,
)
from app.modules.staff.dependencies import AdminStaff, CurrentStaff
from app.modules.users.models import User

router = APIRouter(prefix="/admin", tags=["admin: moderation"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]


def _report_out(report: Report) -> AdminReportOut:
    return AdminReportOut(
        id=report.id,
        reporter_id=report.reporter_id,
        reason=report.reason,
        details=report.details,
        trusted=report.counted_towards_auto_hide,
        status=report.status,
        created_at=report.created_at,
        resolved_at=report.resolved_at,
        resolution_note=report.resolution_note,
    )


def _audit_out(event: AuditEvent) -> AuditEventOut:
    return AuditEventOut(
        id=event.id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        action=str(event.action),
        target_type=event.target_type,
        target_id=event.target_id,
        context=event.context,
        request_id=event.request_id,
        created_at=event.created_at,
    )


async def _accounts(
    session: AsyncSession, ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, AccountSummaryOut]:
    wanted = {user_id for user_id in ids if user_id is not None}
    if not wanted:
        return {}
    users = (await session.execute(select(User).where(User.id.in_(wanted)))).scalars()
    return {user.id: AccountSummaryOut.of(user) for user in users}


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= console.EXCERPT_LENGTH:
        return text
    return text[: console.EXCERPT_LENGTH - 1] + "…"


# ---------------------------------------------------------------------------
# The queue and the content in it
# ---------------------------------------------------------------------------
@router.get(
    "/queue",
    response_model=CursorPage[QueueItemOut],
    summary="The moderation queue",
    description=(
        "Everything waiting for a moderator — reported, auto-hidden or held — one "
        "row per thing, worst reason first, then longest waiting."
    ),
)
async def get_queue(
    staff: CurrentStaff,
    session: SessionDep,
    page: PageDep,
    target_type: Annotated[ReportTargetType | None, Query()] = None,
) -> CursorPage[QueueItemOut]:
    items, next_cursor = await console.queue(
        session, limit=page.limit, cursor=page.cursor, target_type=target_type
    )
    views = [await console.view_target(session, i.target_type, i.target_id) for i in items]
    accounts = await _accounts(session, {view.author_id for view in views})
    return CursorPage(
        items=[
            QueueItemOut(
                target_type=item.target_type,
                target_id=item.target_id,
                priority=item.priority,
                waiting_since=item.waiting_since,
                report_count=item.report_count,
                trusted_report_count=item.trusted_report_count,
                reasons=[ReportReason(reason) for reason in item.reasons],
                held=view.status == "hidden_pending_review",
                excerpt=_excerpt(view.text),
                author=accounts.get(view.author_id) if view.author_id else None,
            )
            for item, view in zip(items, views, strict=True)
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/content/{target_type}/{target_id}",
    response_model=ContentDetailOut,
    summary="Something reported, in full, with its reports",
    description=(
        "The full text, the images, the author's standing, and every report about "
        "it, newest first. Never a location."
    ),
)
async def get_content(
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    staff: CurrentStaff,
    session: SessionDep,
    storage: StorageDep,
) -> ContentDetailOut:
    view = await console.view_target(session, target_type, target_id)
    assets = await load_assets(session, view.media_ids)
    images = [
        ContentImageOut(media_id=media_id, variants=variants)
        for media_id in view.media_ids
        if (variants := image_variants(assets.get(media_id), storage))
    ]
    accounts = await _accounts(session, {view.author_id})
    reports = await console.reports_for(session, target_type, target_id)
    return ContentDetailOut(
        content=ContentOut(
            target_type=view.target_type,
            target_id=view.target_id,
            status=view.status,
            text=view.text,
            created_at=view.created_at,
            images=images,
            details=view.details,
            author=accounts.get(view.author_id) if view.author_id else None,
        ),
        reports=[_report_out(report) for report in reports],
    )


@router.post(
    "/content/{target_type}/{target_id}/remove",
    response_model=ActionOut,
    summary="Take content down",
    description="Upholds the pending reports about it and tells the author.",
)
async def remove_content(
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    payload: RemoveIn,
    staff: CurrentStaff,
    session: SessionDep,
    settings: SettingsDep,
) -> ActionOut:
    resolved = await console.remove_content(
        session,
        staff=staff,
        target_type=target_type,
        target_id=target_id,
        reason=payload.reason,
        settings=settings,
    )
    return ActionOut(reports_resolved=resolved)


@router.post(
    "/content/{target_type}/{target_id}/restore",
    response_model=ActionOut,
    summary="Put hidden, held or removed content back",
    description="Dismisses the pending reports about it.",
)
async def restore_content(
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    payload: NoteIn,
    staff: CurrentStaff,
    session: SessionDep,
) -> ActionOut:
    resolved = await console.restore_content(
        session, staff=staff, target_type=target_type, target_id=target_id, note=payload.note
    )
    return ActionOut(reports_resolved=resolved)


@router.post(
    "/content/{target_type}/{target_id}/resolve",
    response_model=ActionOut,
    summary="Close the reports about something without changing it",
    description=(
        "`dismissed` when nothing was wrong; `upheld` when the problem was dealt "
        "with on the person instead (a warning, a restriction)."
    ),
)
async def resolve_reports(
    target_type: ReportTargetType,
    target_id: uuid.UUID,
    payload: ResolveIn,
    staff: CurrentStaff,
    session: SessionDep,
) -> ActionOut:
    resolved = await console.resolve_reports(
        session,
        staff=staff,
        target_type=target_type,
        target_id=target_id,
        outcome=ReportStatus(payload.outcome),
        note=payload.note,
    )
    return ActionOut(reports_resolved=resolved)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
@router.get(
    "/users/{user_id}",
    response_model=UserHistoryOut,
    summary="A person's moderation history",
    description="Reports by and against them, warnings, and every action taken.",
)
async def get_user_history(
    user_id: uuid.UUID, staff: CurrentStaff, session: SessionDep
) -> UserHistoryOut:
    found = await console.history(session, user_id)
    return UserHistoryOut(
        account=AccountSummaryOut.of(found.user),
        reports_against=[_report_out(report) for report in found.reports_against],
        reports_by=[_report_out(report) for report in found.reports_by],
        warnings=[_warning_out(warning) for warning in found.warnings],
        actions=[_audit_out(event) for event in found.actions],
        content_removed_count=found.upheld_against_content,
    )


def _warning_out(warning: ModerationWarning) -> WarningOut:
    return WarningOut(
        id=warning.id,
        reason=warning.reason,
        report_id=warning.report_id,
        staff_id=warning.staff_id,
        created_at=warning.created_at,
    )


@router.post(
    "/users/{user_id}/warn",
    response_model=WarningOut,
    status_code=status.HTTP_201_CREATED,
    summary="Warn someone",
    description="The reason is shown to them, in their standing and as a notification.",
)
async def warn_user(
    user_id: uuid.UUID,
    payload: WarnIn,
    staff: CurrentStaff,
    session: SessionDep,
    settings: SettingsDep,
) -> WarningOut:
    warning = await console.warn(
        session,
        staff=staff,
        user_id=user_id,
        reason=payload.reason,
        report_id=payload.report_id,
        settings=settings,
    )
    return _warning_out(warning)


@router.post(
    "/users/{user_id}/suspend",
    response_model=AccountSummaryOut,
    summary="Suspend someone",
    description="They keep read access, can see why, and can export their data.",
)
async def suspend_user(
    user_id: uuid.UUID,
    payload: SuspendIn,
    staff: CurrentStaff,
    session: SessionDep,
    settings: SettingsDep,
) -> AccountSummaryOut:
    user = await console.suspend(
        session,
        staff=staff,
        user_id=user_id,
        reason=payload.reason,
        days=payload.days,
        settings=settings,
    )
    return AccountSummaryOut.of(user)


@router.post(
    "/users/{user_id}/unsuspend",
    response_model=AccountSummaryOut,
    summary="Lift a suspension",
    description="Before its end, or one with no end.",
)
async def unsuspend_user(
    user_id: uuid.UUID, staff: CurrentStaff, session: SessionDep
) -> AccountSummaryOut:
    return AccountSummaryOut.of(await console.unsuspend(session, staff=staff, user_id=user_id))


@router.post(
    "/users/{user_id}/restrict",
    response_model=AccountSummaryOut,
    summary="Restrict someone",
    description=(
        "New-account limits whatever their age, no new conversations or job posts, "
        "and new posts held for review. Lapses on its own."
    ),
)
async def restrict_user(
    user_id: uuid.UUID,
    payload: RestrictIn,
    staff: CurrentStaff,
    session: SessionDep,
    settings: SettingsDep,
) -> AccountSummaryOut:
    user = await console.restrict(
        session,
        staff=staff,
        user_id=user_id,
        reason=payload.reason,
        days=payload.days,
        settings=settings,
    )
    return AccountSummaryOut.of(user)


@router.post(
    "/users/{user_id}/unrestrict",
    response_model=AccountSummaryOut,
    summary="Lift a restriction",
    description="Dismisses the pending reports against the person.",
)
async def unrestrict_user(
    user_id: uuid.UUID, payload: NoteIn, staff: CurrentStaff, session: SessionDep
) -> AccountSummaryOut:
    user = await console.unrestrict(session, staff=staff, user_id=user_id, note=payload.note)
    return AccountSummaryOut.of(user)


@router.post(
    "/users/{user_id}/badge/revoke",
    response_model=AccountSummaryOut,
    summary="Take the verified-neighbour badge away",
    description="Locked by default, so the nightly recomputation does not award it back.",
)
async def revoke_badge(
    user_id: uuid.UUID, payload: BadgeRevokeIn, staff: CurrentStaff, session: SessionDep
) -> AccountSummaryOut:
    user = await console.revoke_badge(session, staff=staff, user_id=user_id, lock=payload.lock)
    return AccountSummaryOut.of(user)


@router.post(
    "/users/{user_id}/badge/unlock",
    response_model=AccountSummaryOut,
    summary="Let the badge be earned again",
    description="Removes the lock. The badge returns on the next nightly run if the rule holds.",
)
async def unlock_badge(
    user_id: uuid.UUID, staff: CurrentStaff, session: SessionDep
) -> AccountSummaryOut:
    return AccountSummaryOut.of(await console.unlock_badge(session, staff=staff, user_id=user_id))


@router.post(
    "/users/{user_id}/ban",
    response_model=AccountSummaryOut,
    summary="Ban someone (admin)",
    description=(
        "Signs them out everywhere at once. By default their number and every "
        "device they signed in from join the ban lists."
    ),
)
async def ban_user(
    user_id: uuid.UUID,
    payload: BanIn,
    admin: AdminStaff,
    session: SessionDep,
    settings: SettingsDep,
) -> AccountSummaryOut:
    user = await console.ban(
        session,
        staff=admin,
        user_id=user_id,
        reason=payload.reason,
        ban_phone=payload.ban_phone,
        ban_devices=payload.ban_devices,
        remove_content=payload.remove_content,
        settings=settings,
    )
    return AccountSummaryOut.of(user)


@router.post(
    "/users/{user_id}/unban",
    response_model=AccountSummaryOut,
    summary="Lift a ban (admin)",
    description="Also lifts the phone and device bans it added.",
)
async def unban_user(
    user_id: uuid.UUID, admin: AdminStaff, session: SessionDep
) -> AccountSummaryOut:
    return AccountSummaryOut.of(await console.unban(session, staff=admin, user_id=user_id))


# ---------------------------------------------------------------------------
# The audit log and the ban lists (admin)
# ---------------------------------------------------------------------------
@router.get(
    "/audit",
    response_model=CursorPage[AuditEventOut],
    summary="The audit log (admin)",
    description="Newest first. Filter by actor, action or target.",
)
async def get_audit_log(
    admin: AdminStaff,
    session: SessionDep,
    page: PageDep,
    actor_type: Annotated[AuditActorType | None, Query()] = None,
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[AuditAction | None, Query()] = None,
    target_id: Annotated[uuid.UUID | None, Query()] = None,
) -> CursorPage[AuditEventOut]:
    statement = select(AuditEvent)
    if actor_type is not None:
        statement = statement.where(AuditEvent.actor_type == actor_type)
    if actor_id is not None:
        statement = statement.where(AuditEvent.actor_id == actor_id)
    if action is not None:
        statement = statement.where(AuditEvent.action == action)
    if target_id is not None:
        statement = statement.where(AuditEvent.target_id == target_id)
    rows, next_cursor = await paginate(
        session,
        statement,
        sort_column=AuditEvent.created_at,
        id_column=AuditEvent.id,
        params=page,
    )
    return CursorPage(items=[_audit_out(event) for event in rows], next_cursor=next_cursor)


def _ban_out(row: Any) -> BanEntryOut:
    return BanEntryOut(
        id=row.id,
        user_id=row.user_id,
        reason=row.reason,
        staff_id=row.staff_id,
        created_at=row.created_at,
    )


async def _list_bans(
    session: AsyncSession, model: Any, page: PageParams
) -> CursorPage[BanEntryOut]:
    rows, next_cursor = await paginate(
        session, select(model), sort_column=model.created_at, id_column=model.id, params=page
    )
    return CursorPage(items=[_ban_out(row) for row in rows], next_cursor=next_cursor)


@router.get(
    "/bans/phones",
    response_model=CursorPage[BanEntryOut],
    summary="Banned numbers (admin)",
    description="Newest first. The numbers themselves are not stored, only keyed hashes.",
)
async def list_phone_bans(
    admin: AdminStaff, session: SessionDep, page: PageDep
) -> CursorPage[BanEntryOut]:
    return await _list_bans(session, BannedPhone, page)


@router.post(
    "/bans/phones",
    response_model=BanEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ban a number (admin)",
    description="Stored as a keyed hash. Sign-in from the number fails like a wrong code.",
)
async def ban_phone(
    payload: PhoneBanIn, admin: AdminStaff, session: SessionDep, settings: SettingsDep
) -> BanEntryOut:
    row = await console.ban_phone_number(
        session,
        staff=admin,
        phone_number=normalise_phone_number(payload.phone_number),
        reason=payload.reason,
        settings=settings,
    )
    return _ban_out(row)


@router.delete(
    "/bans/phones/{ban_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Lift a number ban (admin)",
    description="The number can sign in, or sign up, again.",
)
async def lift_phone_ban(ban_id: uuid.UUID, admin: AdminStaff, session: SessionDep) -> None:
    await console.lift_ban_entry(session, staff=admin, model=BannedPhone, ban_id=ban_id)


@router.get(
    "/bans/devices",
    response_model=CursorPage[BanEntryOut],
    summary="Banned devices (admin)",
    description="Newest first. Added by banning an account; stored as keyed hashes.",
)
async def list_device_bans(
    admin: AdminStaff, session: SessionDep, page: PageDep
) -> CursorPage[BanEntryOut]:
    return await _list_bans(session, BannedDevice, page)


@router.delete(
    "/bans/devices/{ban_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Lift a device ban (admin)",
    description="The installation can be used to sign in again.",
)
async def lift_device_ban(ban_id: uuid.UUID, admin: AdminStaff, session: SessionDep) -> None:
    await console.lift_ban_entry(session, staff=admin, model=BannedDevice, ban_id=ban_id)
