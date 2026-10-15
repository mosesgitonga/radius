"""Who may see a group, and who may take part in it.

A leaf module: it reads group tables and nothing else, so the feed can ask "may
this person read this group post?" without importing the groups service — which
imports the feed to create posts. Every other read rule for groups is built on
these functions, so there is one definition of each.

Every refusal to *see* is a 404, as everywhere else: a private group someone is
not in, a group someone is banned from, and a group that does not exist all look
the same.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.modules.groups.models import (
    Group,
    GroupMember,
    GroupMemberStatus,
    GroupRole,
)


def group_not_found() -> NotFoundError:
    return NotFoundError("Group not found")


@dataclass(frozen=True, slots=True)
class GroupView:
    group: Group
    member: GroupMember | None
    """The viewer's row, whatever its status — or None."""

    @property
    def is_active_member(self) -> bool:
        return self.member is not None and self.member.status == GroupMemberStatus.active

    @property
    def role(self) -> GroupRole | None:
        return self.member.role if self.is_active_member and self.member else None


async def membership(
    session: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID
) -> GroupMember | None:
    return (
        await session.execute(
            select(GroupMember)
            .where(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _load(
    session: AsyncSession, group_id: uuid.UUID, viewer_id: uuid.UUID, *, lock: bool
) -> GroupView:
    statement = select(Group).where(Group.id == group_id, Group.deleted_at.is_(None))
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    group = (await session.execute(statement)).scalar_one_or_none()
    if group is None:
        raise group_not_found()
    member = await membership(session, group_id, viewer_id)
    if member is not None and member.status == GroupMemberStatus.banned:
        raise group_not_found()
    return GroupView(group=group, member=member)


async def visible_group(
    session: AsyncSession, *, group_id: uuid.UUID, viewer_id: uuid.UUID, lock: bool = False
) -> GroupView:
    """A group whose *profile* — name, description, rules — the viewer may see.

    Public groups: anyone not banned. Private groups: members, and anyone invited
    or with a pending request, so they can see what they are being asked into.
    """
    view = await _load(session, group_id, viewer_id, lock=lock)
    if not view.group.is_public and view.member is None:
        raise group_not_found()
    return view


async def readable_group(
    session: AsyncSession, *, group_id: uuid.UUID, viewer_id: uuid.UUID, lock: bool = False
) -> GroupView:
    """A group whose *content* — posts, members — the viewer may read.

    Public groups: anyone not banned. Private groups: active members only. An
    invitation shows you the door, not the room.
    """
    view = await _load(session, group_id, viewer_id, lock=lock)
    if not view.group.is_public and not view.is_active_member:
        raise group_not_found()
    return view


async def member_group(
    session: AsyncSession, *, group_id: uuid.UUID, viewer_id: uuid.UUID, lock: bool = False
) -> GroupView:
    """A group the viewer is an active member of, or 404 if they cannot see it and
    403 if they can see it but are not in it."""
    view = await readable_group(session, group_id=group_id, viewer_id=viewer_id, lock=lock)
    if not view.is_active_member:
        raise ForbiddenError("Join the group to do that")
    return view


def is_muted(member: GroupMember, now: datetime | None = None) -> bool:
    return member.muted_until is not None and member.muted_until > (now or datetime.now(UTC))


async def require_participation(
    session: AsyncSession, *, group_id: uuid.UUID, viewer_id: uuid.UUID
) -> GroupView:
    """An active, unmuted member: someone who may post, comment and react."""
    view = await member_group(session, group_id=group_id, viewer_id=viewer_id)
    assert view.member is not None  # noqa: S101 — member_group guarantees it
    if is_muted(view.member):
        raise ForbiddenError(
            "You are muted in this group",
            details={"muted_until": view.member.muted_until.isoformat()}
            if view.member.muted_until
            else {},
        )
    return view
