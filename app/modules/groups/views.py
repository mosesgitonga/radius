"""Turning groups and members into responses, in batches."""

from __future__ import annotations

from collections.abc import Sequence

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.storage import StorageProvider
from app.modules.feed.views import author_view, load_assets, load_users
from app.modules.groups.models import (
    ROLE_RANK,
    Group,
    GroupMember,
    GroupMemberStatus,
    GroupRole,
)
from app.modules.groups.schemas import GroupOut, MemberOut, MembershipOut
from app.modules.location import cells
from app.modules.location.service import area_label_for_cell
from app.modules.media.views import image_variants
from app.modules.users.models import User
from app.modules.users.schemas import AreaLabelOut


async def group_views(
    session: AsyncSession,
    redis: Redis,
    storage: StorageProvider,
    *,
    groups: Sequence[Group],
    viewer: User,
    settings: Settings,
) -> list[GroupOut]:
    if not groups:
        return []
    ids = [group.id for group in groups]
    mine = {
        member.group_id: member
        for member in (
            await session.execute(
                select(GroupMember).where(
                    GroupMember.group_id.in_(ids), GroupMember.user_id == viewer.id
                )
            )
        ).scalars()
    }
    assets = await load_assets(session, [group.cover_media_id for group in groups])

    labels: dict[int, AreaLabelOut] = {}
    for cell in {group.cell_res9 for group in groups if group.cell_res9 is not None}:
        label = await area_label_for_cell(session, redis, cell_res9=cell, settings=settings)
        labels[cell] = AreaLabelOut(label=label.label, precision=label.precision)

    views: list[GroupOut] = []
    for group in groups:
        member = mine.get(group.id)
        active = member is not None and member.status == GroupMemberStatus.active
        bucket = None
        if group.cell_res8 is not None and viewer.home_cell_res8 is not None:
            bucket = cells.bucket_between_cells(viewer.home_cell_res8, group.cell_res8)
        views.append(
            GroupOut(
                id=group.id,
                name=group.name,
                description=group.description,
                category=group.category,
                rules=group.rules,
                visibility=group.visibility,
                join_policy=group.join_policy,
                requires_post_approval=group.requires_post_approval,
                cover=image_variants(
                    assets.get(group.cover_media_id) if group.cover_media_id else None, storage
                ),
                member_count=group.member_count,
                area_label=labels.get(group.cell_res9) if group.cell_res9 is not None else None,
                distance_bucket=bucket,
                my_membership=(
                    MembershipOut(
                        role=member.role,
                        status=member.status,
                        joined_at=member.joined_at,
                        muted_until=member.muted_until,
                    )
                    if member is not None and member.status != GroupMemberStatus.banned
                    else None
                ),
                chat_conversation_id=group.chat_conversation_id if active else None,
                created_at=group.created_at,
            )
        )
    return views


async def member_views(
    session: AsyncSession,
    storage: StorageProvider,
    *,
    members: Sequence[GroupMember],
    viewer_role: GroupRole | None,
) -> list[MemberOut]:
    users = await load_users(session, (member.user_id for member in members))
    assets = await load_assets(session, [user.avatar_media_id for user in users.values()])
    rank = ROLE_RANK[viewer_role] if viewer_role is not None else -1
    sees_mutes = rank >= ROLE_RANK[GroupRole.moderator]
    sees_bans = rank >= ROLE_RANK[GroupRole.admin]
    return [
        MemberOut(
            user=author_view(users[member.user_id], assets, storage),
            role=member.role,
            status=member.status,
            joined_at=member.joined_at,
            muted_until=member.muted_until if sees_mutes else None,
            ban_reason=member.ban_reason if sees_bans else None,
        )
        for member in members
        if member.user_id in users
    ]
