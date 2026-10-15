"""Groups: creating, discovering, joining, administering, and posting.

Every function that changes membership goes through `_set_status`, which keeps
`member_count` and the linked chat in step with the membership row. Every check of
who may do what goes through `access` (who can see) and `permissions` (who can
act). Nothing here re-derives either.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import Select, delete, func, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import RadiusFeature, Settings
from app.core.db import execute_dml
from app.core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
)
from app.core.pagination import PageParams, paginate
from app.core.ratelimit import get_rate_limiter
from app.core.security import generate_opaque_token, hash_secret
from app.modules.auth.dependencies import is_new_account
from app.modules.feed import service as feed_service
from app.modules.feed.models import Post, PostApproval
from app.modules.groups import access
from app.modules.groups.access import GroupView, group_not_found
from app.modules.groups.models import (
    Group,
    GroupInviteLink,
    GroupJoinPolicy,
    GroupMember,
    GroupMemberStatus,
    GroupRole,
    GroupVisibility,
)
from app.modules.groups.permissions import GroupAction, require, require_outranks
from app.modules.location import bands
from app.modules.location.radius import RadiusEngine, RadiusResult
from app.modules.location.service import (
    Anchor,
    AnchorMode,
    home_location_columns,
    resolve_anchor,
)
from app.modules.media.models import MediaPurpose
from app.modules.media.service import claim_assets_for
from app.modules.messaging import service as messaging_service
from app.modules.moderation.audit import record_audit_event
from app.modules.moderation.models import AuditAction, AuditActorType, ContentStatus
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users import service as users_service
from app.modules.users.models import User, UserStatus

logger = logging.getLogger(__name__)

MAX_PINNED_POSTS = 3
MAX_MUTE_DAYS = 365
MAX_SEARCH_RESULTS = 50


# ---------------------------------------------------------------------------
# Membership bookkeeping
# ---------------------------------------------------------------------------
async def _set_status(
    session: AsyncSession,
    group: Group,
    member: GroupMember,
    status: GroupMemberStatus,
) -> None:
    """Change a membership's status, keeping the count and the linked chat in step.

    The count is changed in SQL, so two people joining at once cannot lose an
    increment.
    """
    was_active = member.status == GroupMemberStatus.active
    member.status = status
    now_active = status == GroupMemberStatus.active
    if now_active and member.joined_at is None:
        member.joined_at = datetime.now(UTC)
    await session.flush()
    if was_active == now_active:
        return
    delta = 1 if now_active else -1
    await session.execute(
        update(Group).where(Group.id == group.id).values(member_count=Group.member_count + delta)
    )
    if group.chat_conversation_id is not None:
        await messaging_service.set_linked_membership(
            session,
            conversation_id=group.chat_conversation_id,
            user_id=member.user_id,
            active=now_active,
        )


async def _remove_row(session: AsyncSession, group: Group, member: GroupMember) -> None:
    """Delete a membership — leaving, being removed, a rejected request. Unlike a
    ban, nothing is kept, so the person may come back."""
    await _set_status(session, group, member, GroupMemberStatus.pending)
    await session.delete(member)
    await session.flush()


async def _refresh_group(session: AsyncSession, group: Group) -> Group:
    await session.refresh(group)
    return group


async def _contactable(session: AsyncSession, *, actor: User, user_id: uuid.UUID) -> User:
    """Someone an admin may invite or act on: exists, is live, and is not blocked
    either way. The same 404 as messaging, for the same reasons."""
    other = await users_service.get_user(session, user_id)
    if (
        other is None
        or other.deleted_at is not None
        or other.status in (UserStatus.banned, UserStatus.deletion_pending)
        or await users_service.is_blocked_between(session, actor.id, user_id)
    ):
        raise NotFoundError("User not found")
    return other


async def _admin_ids(session: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(GroupMember.user_id).where(
                    GroupMember.group_id == group_id,
                    GroupMember.status == GroupMemberStatus.active,
                    GroupMember.role.in_((GroupRole.owner, GroupRole.admin)),
                )
            )
        )
        .scalars()
        .all()
    )


async def _hit_daily_limit(user: User, bucket: str, limit: int) -> None:
    result = await get_rate_limiter().hit(
        bucket, f"u:{user.id}", limit=limit, window_seconds=86_400
    )
    if not result.allowed:
        logger.info("rate_limited", extra={"bucket": bucket, "scope": "user"})
        raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)


# ---------------------------------------------------------------------------
# Creating and changing groups
# ---------------------------------------------------------------------------
def _clean_text(value: str | None, *, field: str, max_length: int, required: bool) -> str:
    cleaned = (value or "").strip()
    if required and not cleaned:
        raise BadRequestError(f"A group needs a {field}")
    if len(cleaned) > max_length:
        raise BadRequestError(
            f"That {field} is too long", details={"field": field, "max_length": max_length}
        )
    return cleaned


def _check_category(category: str, settings: Settings) -> str:
    if category not in settings.group_category_list:
        raise BadRequestError(
            "That category does not exist",
            details={"categories": list(settings.group_category_list)},
        )
    return category


def _location_for(user: User) -> dict[str, object]:
    location = home_location_columns(user)
    if location is None:
        raise ConflictError(
            "Set your home area before making a public group",
            details={"action": "PUT /v1/me/location"},
        )
    return location


def _place(group: Group, location: dict[str, object] | None) -> None:
    for column in ("cell_res9", "cell_res8", "cell_res7", "cell_res6", "centroid", "area_id"):
        setattr(group, column, location[column] if location is not None else None)


async def _claim_cover(
    session: AsyncSession, *, owner: User, media_id: uuid.UUID | None
) -> uuid.UUID | None:
    if media_id is None:
        return None
    (asset,) = await claim_assets_for(
        session, media_ids=[media_id], owner_id=owner.id, purpose=MediaPurpose.group
    )
    return asset.id


async def create_group(
    session: AsyncSession,
    *,
    creator: User,
    name: str,
    description: str | None,
    category: str,
    visibility: GroupVisibility,
    join_policy: GroupJoinPolicy,
    requires_post_approval: bool,
    rules: str | None,
    cover_media_id: uuid.UUID | None,
    settings: Settings,
) -> Group:
    """A new group, with its creator as owner.

    A public group is placed at its creator's home, the way a post is — so it is
    discovered by the creator's neighbours, not wherever they happen to be.
    """
    group = Group(
        name=_clean_text(
            name, field="name", max_length=settings.group_name_max_length, required=True
        ),
        description=_clean_text(
            description,
            field="description",
            max_length=settings.group_description_max_length,
            required=False,
        ),
        rules=_clean_text(
            rules, field="rules", max_length=settings.group_rules_max_length, required=False
        )
        or None,
        category=_check_category(category, settings),
        visibility=visibility,
        join_policy=join_policy,
        requires_post_approval=requires_post_approval,
        owner_id=creator.id,
        created_by_id=creator.id,
        member_count=0,
        cover_media_id=await _claim_cover(session, owner=creator, media_id=cover_media_id),
    )
    _place(group, _location_for(creator) if visibility == GroupVisibility.public else None)

    limit = (
        settings.new_account_groups_per_day
        if is_new_account(creator, settings)
        else settings.groups_per_day
    )
    await _hit_daily_limit(creator, "create_group", limit)

    session.add(group)
    await session.flush()
    owner = GroupMember(
        group_id=group.id,
        user_id=creator.id,
        role=GroupRole.owner,
        status=GroupMemberStatus.pending,
    )
    session.add(owner)
    await session.flush()
    await _set_status(session, group, owner, GroupMemberStatus.active)
    logger.info("group_created", extra={"visibility": str(visibility), "category": category})
    return await _refresh_group(session, group)


async def update_group(
    session: AsyncSession,
    *,
    actor: User,
    group_id: uuid.UUID,
    changes: dict[str, Any],
    settings: Settings,
) -> Group:
    """Change a group's settings. Admins and the owner.

    Making a private group public places it at the *acting admin's* home, since a
    private group has no place to keep. Making a public group private forgets its
    place.
    """
    view = await access.member_group(session, group_id=group_id, viewer_id=actor.id, lock=True)
    assert view.role is not None  # noqa: S101 — an active member has a role
    require(view.role, GroupAction.edit_settings)
    group = view.group

    if "name" in changes:
        group.name = _clean_text(
            changes["name"], field="name", max_length=settings.group_name_max_length, required=True
        )
        if group.chat_conversation_id is not None:
            await messaging_service.rename_linked_chat(
                session, conversation_id=group.chat_conversation_id, title=group.name
            )
    if "description" in changes:
        group.description = _clean_text(
            changes["description"],
            field="description",
            max_length=settings.group_description_max_length,
            required=False,
        )
    if "rules" in changes:
        group.rules = (
            _clean_text(
                changes["rules"],
                field="rules",
                max_length=settings.group_rules_max_length,
                required=False,
            )
            or None
        )
    if "category" in changes:
        group.category = _check_category(changes["category"], settings)
    if "join_policy" in changes:
        group.join_policy = GroupJoinPolicy(changes["join_policy"])
    if "requires_post_approval" in changes:
        group.requires_post_approval = bool(changes["requires_post_approval"])
    if "cover_media_id" in changes:
        group.cover_media_id = await _claim_cover(
            session, owner=actor, media_id=changes["cover_media_id"]
        )
    if "visibility" in changes:
        visibility = GroupVisibility(changes["visibility"])
        if visibility != group.visibility:
            _place(group, _location_for(actor) if visibility == GroupVisibility.public else None)
            group.visibility = visibility
    await session.flush()
    return await _refresh_group(session, group)


async def delete_group(session: AsyncSession, *, actor: User, group_id: uuid.UUID) -> None:
    """The owner deletes the group. Soft, as for all user content: its posts stay
    for moderation until retention, but nobody can open it."""
    view = await access.member_group(session, group_id=group_id, viewer_id=actor.id, lock=True)
    assert view.role is not None  # noqa: S101
    require(view.role, GroupAction.delete_group)
    await _soft_delete(session, view.group, actor_id=actor.id)


async def remove_group_for_moderation(session: AsyncSession, *, group_id: uuid.UUID) -> bool:
    """A moderator takes a reported group down: deleted as if by its owner."""
    group = await session.get(Group, group_id, with_for_update=True)
    if group is None or group.deleted_at is not None:
        return False
    await _soft_delete(session, group, actor_id=None)
    return True


async def _soft_delete(session: AsyncSession, group: Group, *, actor_id: uuid.UUID | None) -> None:
    group.deleted_at = datetime.now(UTC)
    if group.chat_conversation_id is not None:
        members = await _active_member_ids(session, group.id)
        for user_id in members:
            await messaging_service.set_linked_membership(
                session, conversation_id=group.chat_conversation_id, user_id=user_id, active=False
            )
    await session.flush()
    await record_audit_event(
        session,
        action=AuditAction.group_deleted,
        actor_type=AuditActorType.user if actor_id else AuditActorType.system,
        actor_id=actor_id,
        target_type="group",
        target_id=group.id,
    )


async def _active_member_ids(session: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(GroupMember.user_id).where(
                    GroupMember.group_id == group_id,
                    GroupMember.status == GroupMemberStatus.active,
                )
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
async def get_group(session: AsyncSession, *, viewer: User, group_id: uuid.UUID) -> GroupView:
    return await access.visible_group(session, group_id=group_id, viewer_id=viewer.id)


async def my_groups(
    session: AsyncSession, *, viewer: User, params: PageParams
) -> tuple[list[Group], str | None]:
    statement = select(Group).where(
        Group.deleted_at.is_(None),
        Group.id.in_(
            select(GroupMember.group_id).where(
                GroupMember.user_id == viewer.id,
                GroupMember.status == GroupMemberStatus.active,
            )
        ),
    )
    return await paginate(
        session, statement, sort_column=Group.updated_at, id_column=Group.id, params=params
    )


async def my_invitations(session: AsyncSession, *, viewer: User) -> list[Group]:
    return list(
        (
            await session.execute(
                select(Group)
                .join(GroupMember, GroupMember.group_id == Group.id)
                .where(
                    Group.deleted_at.is_(None),
                    GroupMember.user_id == viewer.id,
                    GroupMember.status == GroupMemberStatus.invited,
                )
                .order_by(GroupMember.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True, slots=True)
class DiscoveryPage:
    groups: list[Group]
    next_cursor: str | None
    radius: RadiusResult
    radius_km: float
    anchor: Anchor


async def discover(
    session: AsyncSession,
    redis: Redis,
    *,
    viewer: User,
    query: str | None,
    category: str | None,
    distance_km: float | None,
    mode: AnchorMode,
    params: PageParams,
    settings: Settings,
) -> DiscoveryPage:
    """Public groups near the viewer.

    Within the groups radius — twice the feed radius — or the viewer's own distance
    filter, up to its cap. Private groups never appear; neither do groups the viewer
    is banned from. With a search, the best word matches come first and there is
    one page; without, the busiest groups come first, paginated.
    """
    anchor = await resolve_anchor(redis, user=viewer, mode=mode)
    if anchor is None:
        raise ConflictError(
            "Set your home area to find groups nearby",
            details={"action": "PUT /v1/me/location"},
        )
    engine = RadiusEngine(session, redis, settings)
    radius = await engine.radius_for(RadiusFeature.groups, anchor.cell_res8)
    radius_km = engine.effective_radius_km(radius, distance_km)

    banned_from = select(GroupMember.group_id).where(
        GroupMember.user_id == viewer.id, GroupMember.status == GroupMemberStatus.banned
    )
    statement: Select[Group] = select(Group).where(
        Group.visibility == GroupVisibility.public,
        Group.deleted_at.is_(None),
        Group.id.not_in(banned_from),
        bands.within(Group.centroid, anchor, int(radius_km * 1000)),
    )
    if category is not None:
        statement = statement.where(Group.category == _check_category(category, settings))

    cleaned = (query or "").strip()
    if cleaned:
        # `<%` is word similarity, served by the trigram index; its threshold is a
        # setting, applied for this transaction only.
        await session.execute(
            select(
                func.set_config(
                    "pg_trgm.word_similarity_threshold",
                    str(settings.group_search_min_similarity),
                    True,
                )
            )
        )
        score = func.word_similarity(literal(cleaned), Group.name)
        rows = (
            await session.execute(
                statement.where(literal(cleaned).op("<%")(Group.name))
                .order_by(score.desc(), Group.member_count.desc(), Group.id)
                .limit(min(params.limit, MAX_SEARCH_RESULTS))
            )
        ).scalars()
        return DiscoveryPage(
            groups=list(rows), next_cursor=None, radius=radius, radius_km=radius_km, anchor=anchor
        )

    groups, next_cursor = await paginate(
        session, statement, sort_column=Group.member_count, id_column=Group.id, params=params
    )
    return DiscoveryPage(
        groups=groups, next_cursor=next_cursor, radius=radius, radius_km=radius_km, anchor=anchor
    )


async def list_members(
    session: AsyncSession,
    *,
    viewer: User,
    group_id: uuid.UUID,
    status: GroupMemberStatus,
    params: PageParams,
) -> tuple[list[GroupMember], str | None]:
    """Members, for anyone who can read the group. Join requests, invitations and
    bans are for the group's admins. Anyone blocked either way is left out."""
    view = await access.readable_group(session, group_id=group_id, viewer_id=viewer.id)
    if status != GroupMemberStatus.active:
        if view.role is None:
            raise ForbiddenError("Only the group's admins can see that list")
        action = (
            GroupAction.ban_members
            if status == GroupMemberStatus.banned
            else GroupAction.review_join_requests
        )
        require(view.role, action)
    statement = select(GroupMember).where(
        GroupMember.group_id == group_id, GroupMember.status == status
    )
    hidden = await users_service.blocked_user_ids(session, viewer.id)
    if hidden:
        statement = statement.where(GroupMember.user_id.not_in(hidden))
    return await paginate(
        session,
        statement,
        sort_column=GroupMember.created_at,
        id_column=GroupMember.id,
        params=params,
        descending=False,
    )


# ---------------------------------------------------------------------------
# Joining and leaving
# ---------------------------------------------------------------------------
async def join_group(
    session: AsyncSession, *, user: User, group_id: uuid.UUID, settings: Settings
) -> GroupMember:
    """Join a public group, or ask to.

    Private groups cannot be joined this way: they are invite-only, and a 404 here
    says nothing about whether one exists. Accepting an invitation goes through
    `respond_to_invite`; an invite link through `join_with_link`.
    """
    view = await access.visible_group(session, group_id=group_id, viewer_id=user.id, lock=True)
    group, member = view.group, view.member
    if not group.is_public:
        raise group_not_found()
    if member is not None and member.status in (
        GroupMemberStatus.active,
        GroupMemberStatus.pending,
    ):
        return member
    if member is not None and member.status == GroupMemberStatus.invited:
        await _set_status(session, group, member, GroupMemberStatus.active)
        return member

    if group.join_policy == GroupJoinPolicy.approval:
        await _hit_daily_limit(user, "group_join_request", settings.group_join_requests_per_day)
    member = GroupMember(group_id=group.id, user_id=user.id, status=GroupMemberStatus.pending)
    try:
        async with session.begin_nested():
            session.add(member)
    except IntegrityError:
        # A double tap: the unique index caught the second.
        existing = await access.membership(session, group.id, user.id)
        if existing is None:  # pragma: no cover
            raise
        return existing

    if group.join_policy == GroupJoinPolicy.open:
        await _set_status(session, group, member, GroupMemberStatus.active)
    else:
        for admin_id in await _admin_ids(session, group.id):
            await notifications_service.notify(
                session,
                recipient_id=admin_id,
                kind=NotificationType.group_join_request,
                actor_id=user.id,
                target_type="group",
                target_id=group.id,
                collapse_key=f"group_join_request:{group.id}",
                data={"group_id": str(group.id)},
                settings=settings,
            )
    return member


async def leave_group(session: AsyncSession, *, user: User, group_id: uuid.UUID) -> None:
    """Leave, cancel a join request, or decline an invitation.

    An owner who leaves hands the group on — to an admin, then a moderator, then
    the longest-standing member. If nobody is left, the group is deleted.
    """
    view = await access.visible_group(session, group_id=group_id, viewer_id=user.id, lock=True)
    member = view.member
    if member is None:
        raise NotFoundError("You are not in this group")
    was_owner = member.role == GroupRole.owner and member.status == GroupMemberStatus.active
    await _remove_row(session, view.group, member)
    if was_owner:
        await _hand_on(session, view.group)


async def _hand_on(session: AsyncSession, group: Group) -> None:
    successor = (
        await session.execute(
            select(GroupMember)
            .where(
                GroupMember.group_id == group.id,
                GroupMember.status == GroupMemberStatus.active,
            )
            .order_by(
                (GroupMember.role == GroupRole.admin).desc(),
                (GroupMember.role == GroupRole.moderator).desc(),
                GroupMember.joined_at.asc().nulls_last(),
                GroupMember.created_at.asc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if successor is None:
        group.owner_id = None
        await _soft_delete(session, group, actor_id=None)
        return
    successor.role = GroupRole.owner
    group.owner_id = successor.user_id
    await session.flush()
    await record_audit_event(
        session,
        action=AuditAction.group_ownership_transferred,
        actor_type=AuditActorType.system,
        actor_id=None,
        target_type="group",
        target_id=group.id,
        context={"to": str(successor.user_id), "reason": "owner_left"},
    )


# ---------------------------------------------------------------------------
# Join requests and invitations
# ---------------------------------------------------------------------------
async def _admin_view(
    session: AsyncSession, *, actor: User, group_id: uuid.UUID, action: GroupAction
) -> GroupView:
    view = await access.member_group(session, group_id=group_id, viewer_id=actor.id, lock=True)
    assert view.role is not None  # noqa: S101
    require(view.role, action)
    return view


async def _target(
    session: AsyncSession,
    group: Group,
    user_id: uuid.UUID,
    *,
    statuses: Sequence[GroupMemberStatus],
) -> GroupMember:
    member = await access.membership(session, group.id, user_id)
    if member is None or member.status not in statuses:
        raise NotFoundError("That person is not in this group")
    return member


async def review_join_request(
    session: AsyncSession,
    *,
    actor: User,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    approve: bool,
    settings: Settings,
) -> None:
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.review_join_requests
    )
    member = await _target(session, view.group, user_id, statuses=(GroupMemberStatus.pending,))
    if not approve:
        await _remove_row(session, view.group, member)
        return
    await _set_status(session, view.group, member, GroupMemberStatus.active)
    await notifications_service.notify(
        session,
        recipient_id=user_id,
        kind=NotificationType.group_join_approved,
        actor_id=actor.id,
        target_type="group",
        target_id=view.group.id,
        collapse_key=f"group_join_approved:{view.group.id}",
        data={"group_id": str(view.group.id)},
        settings=settings,
    )


async def invite(
    session: AsyncSession,
    *,
    actor: User,
    group_id: uuid.UUID,
    user_ids: Sequence[uuid.UUID],
    settings: Settings,
) -> list[uuid.UUID]:
    """Invite people directly. Returns who was newly invited.

    Someone with a pending request is simply let in — the admin has answered it.
    Someone banned is refused: unban them first, deliberately, rather than letting
    an invitation quietly lift a ban another admin placed.
    """
    view = await _admin_view(session, actor=actor, group_id=group_id, action=GroupAction.invite)
    group = view.group
    wanted = list(dict.fromkeys(user_id for user_id in user_ids if user_id != actor.id))
    if not wanted:
        raise BadRequestError("Invite at least one other person")
    limit = (
        settings.new_account_group_invites_per_day
        if is_new_account(actor, settings)
        else settings.group_invites_per_day
    )
    result = await get_rate_limiter().hit(
        "group_invite", f"u:{actor.id}", limit=limit, window_seconds=86_400, cost=len(wanted)
    )
    if not result.allowed:
        raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)

    invited: list[uuid.UUID] = []
    for user_id in wanted:
        await _contactable(session, actor=actor, user_id=user_id)
        existing = await access.membership(session, group.id, user_id)
        if existing is not None:
            if existing.status == GroupMemberStatus.banned:
                raise ConflictError(
                    "That person is banned from this group; unban them first",
                    details={"user_id": str(user_id)},
                )
            if existing.status == GroupMemberStatus.pending:
                await _set_status(session, group, existing, GroupMemberStatus.active)
            continue
        session.add(
            GroupMember(
                group_id=group.id,
                user_id=user_id,
                status=GroupMemberStatus.invited,
                invited_by_id=actor.id,
            )
        )
        invited.append(user_id)
    await session.flush()
    for user_id in invited:
        await notifications_service.notify(
            session,
            recipient_id=user_id,
            kind=NotificationType.group_invite,
            actor_id=actor.id,
            target_type="group",
            target_id=group.id,
            collapse_key=f"group_invite:{group.id}",
            data={"group_id": str(group.id)},
            settings=settings,
        )
    return invited


async def respond_to_invite(
    session: AsyncSession, *, user: User, group_id: uuid.UUID, accept: bool
) -> GroupView:
    view = await access.visible_group(session, group_id=group_id, viewer_id=user.id, lock=True)
    member = view.member
    if member is None or member.status != GroupMemberStatus.invited:
        raise NotFoundError("There is no invitation to this group")
    if accept:
        await _set_status(session, view.group, member, GroupMemberStatus.active)
    else:
        await _remove_row(session, view.group, member)
    return GroupView(
        group=await _refresh_group(session, view.group), member=member if accept else None
    )


# ---------------------------------------------------------------------------
# Invite links
# ---------------------------------------------------------------------------
async def create_invite_link(
    session: AsyncSession,
    *,
    actor: User,
    group_id: uuid.UUID,
    expires_in_days: int | None,
    max_uses: int | None,
    settings: Settings,
) -> tuple[GroupInviteLink, str]:
    """A link into the group. The token is returned once and stored only hashed."""
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.manage_invite_links
    )
    days = expires_in_days or settings.group_invite_link_default_days
    if days > settings.group_invite_link_max_days:
        raise BadRequestError(
            "That link would last too long",
            details={"max_days": settings.group_invite_link_max_days},
        )
    token = generate_opaque_token()
    link = GroupInviteLink(
        group_id=view.group.id,
        token_hash=hash_secret(token, settings),
        created_by_id=actor.id,
        expires_at=datetime.now(UTC) + timedelta(days=days),
        max_uses=max_uses,
    )
    session.add(link)
    await session.flush()
    return link, token


async def list_invite_links(
    session: AsyncSession, *, actor: User, group_id: uuid.UUID
) -> list[GroupInviteLink]:
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.manage_invite_links
    )
    return list(
        (
            await session.execute(
                select(GroupInviteLink)
                .where(
                    GroupInviteLink.group_id == view.group.id,
                    GroupInviteLink.revoked_at.is_(None),
                    GroupInviteLink.expires_at > datetime.now(UTC),
                )
                .order_by(GroupInviteLink.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def revoke_invite_link(
    session: AsyncSession, *, actor: User, group_id: uuid.UUID, link_id: uuid.UUID
) -> None:
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.manage_invite_links
    )
    link = await session.get(GroupInviteLink, link_id)
    if link is None or link.group_id != view.group.id:
        raise NotFoundError("Invite link not found")
    if link.revoked_at is None:
        link.revoked_at = datetime.now(UTC)
        await session.flush()


async def _usable_link(session: AsyncSession, *, token: str, settings: Settings) -> GroupInviteLink:
    """One 404 for every way a link can be no good — unknown, expired, used up,
    revoked, or its group gone — so a link cannot be probed."""
    link = (
        await session.execute(
            select(GroupInviteLink).where(
                GroupInviteLink.token_hash == hash_secret(token, settings)
            )
        )
    ).scalar_one_or_none()
    if link is None or not link.is_usable(datetime.now(UTC)):
        raise NotFoundError("That invite link is not valid")
    return link


async def preview_invite_link(
    session: AsyncSession, *, viewer: User, token: str, settings: Settings
) -> Group:
    """What a link leads to: the group's profile, never its posts."""
    link = await _usable_link(session, token=token, settings=settings)
    group = await session.get(Group, link.group_id)
    member = await access.membership(session, link.group_id, viewer.id)
    if (
        group is None
        or group.deleted_at is not None
        or (member is not None and member.status == GroupMemberStatus.banned)
    ):
        raise NotFoundError("That invite link is not valid")
    return group


async def join_with_link(
    session: AsyncSession, *, user: User, token: str, settings: Settings
) -> GroupView:
    """Join through a link: straight in, whatever the join policy, because an admin
    made the link. A banned person is refused like an invalid link."""
    link = await _usable_link(session, token=token, settings=settings)
    try:
        view = await access.visible_group(
            session, group_id=link.group_id, viewer_id=user.id, lock=True
        )
    except NotFoundError:
        # Private groups 404 for non-members in visible_group; a link is the
        # exception, so look again with the link as the permission.
        view = await _link_view(session, link=link, user=user)
    member = view.member
    if member is not None and member.status == GroupMemberStatus.active:
        return view

    # Count the use atomically, so a link limited to ten uses admits ten people
    # however many tap it at once.
    claimed = await execute_dml(
        session,
        update(GroupInviteLink)
        .where(
            GroupInviteLink.id == link.id,
            or_(
                GroupInviteLink.max_uses.is_(None),
                GroupInviteLink.use_count < GroupInviteLink.max_uses,
            ),
        )
        .values(use_count=GroupInviteLink.use_count + 1),
    )
    if not claimed:
        raise NotFoundError("That invite link is not valid")

    if member is None:
        member = GroupMember(
            group_id=view.group.id,
            user_id=user.id,
            status=GroupMemberStatus.pending,
            invited_by_id=link.created_by_id,
        )
        session.add(member)
        await session.flush()
    await _set_status(session, view.group, member, GroupMemberStatus.active)
    return GroupView(group=await _refresh_group(session, view.group), member=member)


async def _link_view(session: AsyncSession, *, link: GroupInviteLink, user: User) -> GroupView:
    group = (
        await session.execute(
            select(Group)
            .where(Group.id == link.group_id, Group.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    member = await access.membership(session, link.group_id, user.id)
    if group is None or (member is not None and member.status == GroupMemberStatus.banned):
        raise NotFoundError("That invite link is not valid")
    return GroupView(group=group, member=member)


# ---------------------------------------------------------------------------
# Admin tools
# ---------------------------------------------------------------------------
async def remove_member(
    session: AsyncSession, *, actor: User, group_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Remove someone. Unlike a ban, they may come back."""
    if user_id == actor.id:
        raise BadRequestError("Leave the group instead")
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.remove_members
    )
    assert view.role is not None  # noqa: S101
    target = await _target(
        session,
        view.group,
        user_id,
        statuses=(GroupMemberStatus.active, GroupMemberStatus.invited, GroupMemberStatus.pending),
    )
    require_outranks(view.role, target.role)
    await _remove_row(session, view.group, target)
    await record_audit_event(
        session,
        action=AuditAction.group_member_removed,
        actor_id=actor.id,
        target_type="group",
        target_id=view.group.id,
        context={"user_id": str(user_id)},
    )


async def ban_member(
    session: AsyncSession,
    *,
    actor: User,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    reason: str | None,
) -> None:
    """Ban someone, member or not. They lose access at once and cannot come back by
    any route — joining, asking, an invitation, or a link — until unbanned."""
    if user_id == actor.id:
        raise BadRequestError("You cannot ban yourself")
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.ban_members
    )
    assert view.role is not None  # noqa: S101
    target = await access.membership(session, view.group.id, user_id)
    if target is not None and target.status == GroupMemberStatus.banned:
        return
    if target is not None:
        require_outranks(view.role, target.role)
    else:
        if await users_service.get_user(session, user_id) is None:
            raise NotFoundError("User not found")
        target = GroupMember(
            group_id=view.group.id, user_id=user_id, status=GroupMemberStatus.pending
        )
        session.add(target)
    target.role = GroupRole.member
    target.banned_at = datetime.now(UTC)
    target.banned_by_id = actor.id
    target.ban_reason = (reason or "").strip()[:500] or None
    target.muted_until = None
    await _set_status(session, view.group, target, GroupMemberStatus.banned)
    await record_audit_event(
        session,
        action=AuditAction.group_member_banned,
        actor_id=actor.id,
        target_type="group",
        target_id=view.group.id,
        context={"user_id": str(user_id)},
    )


async def unban_member(
    session: AsyncSession, *, actor: User, group_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.ban_members
    )
    target = await _target(session, view.group, user_id, statuses=(GroupMemberStatus.banned,))
    await session.delete(target)
    await session.flush()
    await record_audit_event(
        session,
        action=AuditAction.group_member_unbanned,
        actor_id=actor.id,
        target_type="group",
        target_id=view.group.id,
        context={"user_id": str(user_id)},
    )


async def mute_member(
    session: AsyncSession,
    *,
    actor: User,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    until: datetime | None,
) -> None:
    """Mute until a time, or unmute with None. A muted member reads but cannot post,
    comment or react in the group."""
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.mute_members
    )
    assert view.role is not None  # noqa: S101
    target = await _target(session, view.group, user_id, statuses=(GroupMemberStatus.active,))
    require_outranks(view.role, target.role)
    if until is not None:
        now = datetime.now(UTC)
        if until <= now:
            raise BadRequestError("A mute must end in the future")
        if until > now + timedelta(days=MAX_MUTE_DAYS):
            raise BadRequestError(
                "A mute can last at most a year", details={"max_days": MAX_MUTE_DAYS}
            )
    target.muted_until = until
    await session.flush()


async def set_role(
    session: AsyncSession,
    *,
    actor: User,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    role: GroupRole,
) -> None:
    """Promote, demote, or hand over the group.

    Admins appoint and remove moderators. Only the owner makes or unmakes admins,
    and giving someone `owner` hands the group over: the previous owner becomes an
    admin.
    """
    if user_id == actor.id:
        raise BadRequestError("You cannot change your own role")
    view = await access.member_group(session, group_id=group_id, viewer_id=actor.id, lock=True)
    assert view.role is not None  # noqa: S101
    target = await _target(session, view.group, user_id, statuses=(GroupMemberStatus.active,))
    if target.role == role:
        return

    if role == GroupRole.owner:
        require(view.role, GroupAction.transfer_ownership)
    elif GroupRole.admin in (role, target.role):
        require(view.role, GroupAction.appoint_admins)
    else:
        require(view.role, GroupAction.appoint_moderators)
    require_outranks(view.role, target.role)

    previous = target.role
    target.role = role
    if role == GroupRole.owner:
        assert view.member is not None  # noqa: S101
        view.member.role = GroupRole.admin
        view.group.owner_id = user_id
    await session.flush()
    await record_audit_event(
        session,
        action=(
            AuditAction.group_ownership_transferred
            if role == GroupRole.owner
            else AuditAction.group_role_changed
        ),
        actor_id=actor.id,
        target_type="group",
        target_id=view.group.id,
        context={"user_id": str(user_id), "from": previous.value, "to": role.value},
    )


async def enable_chat(session: AsyncSession, *, actor: User, group_id: uuid.UUID) -> Group:
    """Give the group a linked chat, with every active member in it."""
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.enable_chat
    )
    group = view.group
    if group.chat_conversation_id is not None:
        return group
    conversation = await messaging_service.create_linked_chat(
        session,
        group_id=group.id,
        title=group.name,
        owner_id=group.owner_id or actor.id,
        member_ids=await _active_member_ids(session, group.id),
    )
    group.chat_conversation_id = conversation.id
    await session.flush()
    return await _refresh_group(session, group)


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------
async def create_group_post(
    session: AsyncSession,
    redis: Redis,
    *,
    author: User,
    group_id: uuid.UUID,
    body: str,
    category: str,
    media_ids: list[uuid.UUID],
    settings: Settings,
) -> Post:
    """Post in a group. Needs approval if the group asks for it — unless the author
    is one of the people who would approve it."""
    view = await access.require_participation(session, group_id=group_id, viewer_id=author.id)
    assert view.role is not None  # noqa: S101
    needs_approval = view.group.requires_post_approval and view.role == GroupRole.member
    post, _reach = await feed_service.create_post(
        session,
        redis,
        author=author,
        body=body,
        category=category,
        media_ids=media_ids,
        settings=settings,
        group=view.group,
        needs_approval=needs_approval,
    )
    return post


@dataclass(frozen=True, slots=True)
class GroupPostPage:
    posts: list[Post]
    pinned: list[Post]
    next_cursor: str | None


def _readable_posts(group_id: uuid.UUID) -> Select[Post]:
    return select(Post).where(
        Post.group_id == group_id,
        Post.deleted_at.is_(None),
        Post.status != ContentStatus.removed,
    )


async def list_group_posts(
    session: AsyncSession, *, viewer: User, group_id: uuid.UUID, params: PageParams
) -> GroupPostPage:
    """Approved posts, newest first, plus the viewer's own pending ones; pinned posts
    separately on the first page. Blocked authors are left out."""
    await access.readable_group(session, group_id=group_id, viewer_id=viewer.id)
    hidden = await users_service.blocked_user_ids(session, viewer.id)

    statement = _readable_posts(group_id).where(
        or_(
            Post.approval_status.is_(None),
            Post.approval_status == PostApproval.approved,
            Post.author_id == viewer.id,
        ),
        or_(Post.status == ContentStatus.visible, Post.author_id == viewer.id),
    )
    if hidden:
        statement = statement.where(Post.author_id.not_in(hidden))

    pinned: list[Post] = []
    if params.cursor is None:
        pinned = list(
            (
                await session.execute(
                    statement.where(Post.pinned_at.is_not(None)).order_by(Post.pinned_at.desc())
                )
            )
            .scalars()
            .all()
        )
    posts, next_cursor = await paginate(
        session,
        statement.where(Post.pinned_at.is_(None)),
        sort_column=Post.created_at,
        id_column=Post.id,
        params=params,
    )
    return GroupPostPage(posts=posts, pinned=pinned, next_cursor=next_cursor)


async def pending_posts(
    session: AsyncSession, *, viewer: User, group_id: uuid.UUID, params: PageParams
) -> tuple[list[Post], str | None]:
    view = await _admin_view(
        session, actor=viewer, group_id=group_id, action=GroupAction.review_posts
    )
    return await paginate(
        session,
        _readable_posts(view.group.id).where(Post.approval_status == PostApproval.pending),
        sort_column=Post.created_at,
        id_column=Post.id,
        params=params,
        descending=False,
    )


async def _group_post(session: AsyncSession, group: Group, post_id: uuid.UUID) -> Post:
    post = (
        await session.execute(
            _readable_posts(group.id)
            .where(Post.id == post_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if post is None:
        raise NotFoundError("Post not found")
    return post


async def review_post(
    session: AsyncSession,
    *,
    actor: User,
    group_id: uuid.UUID,
    post_id: uuid.UUID,
    approve: bool,
    settings: Settings,
) -> Post:
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.review_posts
    )
    post = await _group_post(session, view.group, post_id)
    if post.approval_status != PostApproval.pending:
        raise ConflictError("That post is not waiting for approval")
    post.approval_status = PostApproval.approved if approve else PostApproval.rejected
    await session.flush()
    if approve:
        await notifications_service.notify(
            session,
            recipient_id=post.author_id,
            kind=NotificationType.group_post_approved,
            actor_id=actor.id,
            target_type="post",
            target_id=post.id,
            collapse_key=f"group_post_approved:{post.id}",
            data={"group_id": str(view.group.id), "post_id": str(post.id)},
            settings=settings,
        )
    return post


async def set_pinned(
    session: AsyncSession, *, actor: User, group_id: uuid.UUID, post_id: uuid.UUID, pinned: bool
) -> Post:
    view = await _admin_view(session, actor=actor, group_id=group_id, action=GroupAction.pin_posts)
    post = await _group_post(session, view.group, post_id)
    if pinned and post.pinned_at is None:
        if post.approval_status not in (None, PostApproval.approved):
            raise ConflictError("Only an approved post can be pinned")
        count = await session.scalar(
            select(func.count(Post.id)).where(
                Post.group_id == view.group.id,
                Post.pinned_at.is_not(None),
                Post.deleted_at.is_(None),
            )
        )
        if (count or 0) >= MAX_PINNED_POSTS:
            raise ConflictError("Unpin a post first", details={"max_pinned": MAX_PINNED_POSTS})
        post.pinned_at = datetime.now(UTC)
    elif not pinned:
        post.pinned_at = None
    await session.flush()
    return post


async def remove_post(
    session: AsyncSession, *, actor: User, group_id: uuid.UUID, post_id: uuid.UUID
) -> None:
    """A moderator removes someone's post from the group. Soft, as every deletion
    is, so a report against it keeps its evidence."""
    view = await _admin_view(
        session, actor=actor, group_id=group_id, action=GroupAction.remove_posts
    )
    post = await _group_post(session, view.group, post_id)
    post.deleted_at = datetime.now(UTC)
    post.pinned_at = None
    await session.flush()
    if post.author_id != actor.id:
        await record_audit_event(
            session,
            action=AuditAction.group_post_removed,
            actor_id=actor.id,
            target_type="post",
            target_id=post.id,
            context={"group_id": str(view.group.id), "author_id": str(post.author_id)},
        )


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def purge_user_groups(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, int]:
    """Take a purged account out of every group, passing on any it owned.

    Bans placed *on* them are deleted with everything else: a ban is about an
    account, and the account no longer exists. Their posts go with the feed purge.
    """
    rows = list(
        (await session.execute(select(GroupMember).where(GroupMember.user_id == user_id)))
        .scalars()
        .all()
    )
    handed_on = 0
    for member in rows:
        group = (
            await session.execute(
                select(Group).where(Group.id == member.group_id).with_for_update()
            )
        ).scalar_one()
        was_owner = member.role == GroupRole.owner and member.status == GroupMemberStatus.active
        await _remove_row(session, group, member)
        if was_owner and group.deleted_at is None:
            await _hand_on(session, group)
            handed_on += 1
    await execute_dml(
        session,
        update(Group).where(Group.created_by_id == user_id).values(created_by_id=None),
    )
    await execute_dml(
        session,
        update(GroupInviteLink)
        .where(GroupInviteLink.created_by_id == user_id, GroupInviteLink.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC)),
    )
    return {"memberships": len(rows), "groups_handed_on": handed_on}


async def export_user_groups(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    """The groups a person is in, and those they created.

    Bans placed on them are not listed: a ban's existence is the group's record,
    and the ban reason is written for the group's admins.
    """
    memberships = (
        await session.execute(
            select(GroupMember, Group)
            .join(Group, Group.id == GroupMember.group_id)
            .where(
                GroupMember.user_id == user_id,
                GroupMember.status != GroupMemberStatus.banned,
            )
            .order_by(GroupMember.created_at.asc())
        )
    ).all()
    created = (
        await session.execute(
            select(Group).where(Group.created_by_id == user_id).order_by(Group.created_at.asc())
        )
    ).scalars()
    return {
        "memberships": [
            {
                "group_id": str(group.id),
                "group_name": group.name,
                "role": str(member.role),
                "status": str(member.status),
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "muted_until": member.muted_until.isoformat() if member.muted_until else None,
            }
            for member, group in memberships
        ],
        "groups_created": [
            {
                "id": str(group.id),
                "name": group.name,
                "visibility": str(group.visibility),
                "created_at": group.created_at.isoformat(),
                "deleted_at": group.deleted_at.isoformat() if group.deleted_at else None,
            }
            for group in created
        ],
    }


async def delete_old_invite_links(session: AsyncSession, *, retain_days: int = 30) -> int:
    """Expired and revoked links, a while after they stopped working."""
    cutoff = datetime.now(UTC) - timedelta(days=retain_days)
    return await execute_dml(
        session,
        delete(GroupInviteLink).where(
            or_(GroupInviteLink.expires_at < cutoff, GroupInviteLink.revoked_at < cutoff)
        ),
    )
