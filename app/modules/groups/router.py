"""Group endpoints: discovery, membership, admin tools, and group posts.

Refusals follow one rule. A group the caller may not *see* — private and not a
member, banned, or nonexistent — is a 404. A group they can see but lack the role
for is a 403, because they already know it exists and the useful answer is "you
can't do that here".
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.db import SessionDep
from app.core.idempotency import IDEMPOTENCY_HEADER, execute_idempotently
from app.core.pagination import PageParams, page_params
from app.core.providers.storage import StorageProvider, get_storage_provider
from app.core.redis import get_redis
from app.modules.auth.dependencies import (
    ActiveUser,
    CurrentUser,
    tiered_user_rate_limit,
    user_rate_limit,
)
from app.modules.feed import views as feed_views
from app.modules.feed.models import Post
from app.modules.feed.schemas import PostCreateIn, PostOut
from app.modules.groups import service as groups_service
from app.modules.groups import views
from app.modules.groups.access import GroupView
from app.modules.groups.models import Group, GroupMemberStatus
from app.modules.groups.schemas import (
    BanIn,
    DiscoveryOut,
    GroupCreateIn,
    GroupOut,
    GroupPageOut,
    GroupPostPageOut,
    GroupUpdateIn,
    InvitedOut,
    InviteIn,
    InviteLinkCreateIn,
    InviteLinkOut,
    MemberPageOut,
    MuteIn,
    PinIn,
    RespondIn,
    ReviewIn,
    RoleIn,
)
from app.modules.users.models import User

router = APIRouter(prefix="/groups", tags=["groups"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]
StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
PageDep = Annotated[PageParams, Depends(page_params)]

_post_limit = tiered_user_rate_limit(
    # The same bucket as neighbourhood posts: a group is not a way round the limit.
    bucket="create_post",
    window_seconds=86_400,
    new_account_limit=lambda s: s.new_account_posts_per_day,
    established_limit=lambda s: s.posts_per_day,
)
_admin_limit = user_rate_limit(bucket="group_admin", limit=300, window_seconds=3600)

NOT_FOUND_DOC = (
    "\n\nA group the caller may not see — private and not a member, banned, or "
    "nonexistent — is a 404. One they can see but lack the role for is a 403."
)


async def _one(
    session: SessionDep,
    redis: Redis,
    storage: StorageProvider,
    group: Group,
    viewer: User,
    settings: Settings,
) -> GroupOut:
    (rendered,) = await views.group_views(
        session, redis, storage, groups=[group], viewer=viewer, settings=settings
    )
    return rendered


async def _posts(
    session: SessionDep,
    redis: Redis,
    storage: StorageProvider,
    posts: list[Post],
    viewer: User,
    settings: Settings,
) -> list[PostOut]:
    return await feed_views.post_views(
        session,
        redis,
        storage,
        posts=posts,
        viewer=viewer,
        viewer_cell_res8=viewer.home_cell_res8,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=GroupOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a group",
    description=(
        "The caller becomes its owner. A **public** group is placed at the caller's "
        "home area and is discoverable by neighbours within the groups radius; a "
        "**private** group has no place and can only be joined by invitation.\n\n"
        "New accounts can create one group a day."
    ),
)
async def create_group(
    payload: GroupCreateIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GroupOut:
    group = await groups_service.create_group(
        session,
        creator=user,
        name=payload.name,
        description=payload.description,
        category=payload.category,
        visibility=payload.visibility,
        join_policy=payload.join_policy,
        requires_post_approval=payload.requires_post_approval,
        rules=payload.rules,
        cover_media_id=payload.cover_media_id,
        settings=settings,
    )
    return await _one(session, redis, storage, group, user, settings)


@router.get(
    "/discover",
    response_model=DiscoveryOut,
    summary="Find public groups nearby",
    description=(
        "Public groups within the caller's groups radius (twice the feed radius), "
        "or their own `distance_km` up to its cap. Search by name with `q` — "
        "typo-tolerant — and filter by `category`. Private groups never appear.\n\n"
        "With `q`: the best matches, one page. Without: the busiest groups first, "
        "paginated."
    ),
)
async def discover(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: str | None = None,
    distance_km: Annotated[float | None, Query(gt=0, le=100)] = None,
    anchor: Annotated[Literal["home", "explore"], Query()] = "home",
) -> DiscoveryOut:
    result = await groups_service.discover(
        session,
        redis,
        viewer=user,
        query=q,
        category=category,
        distance_km=distance_km,
        mode=anchor,
        params=page,
        settings=settings,
    )
    return DiscoveryOut(
        items=await views.group_views(
            session, redis, storage, groups=result.groups, viewer=user, settings=settings
        ),
        next_cursor=result.next_cursor,
        radius_km=round(result.radius_km * 2) / 2,
    )


@router.get(
    "/mine",
    response_model=GroupPageOut,
    summary="The caller's groups",
    description="Groups the caller is an active member of, most recently active first.",
)
async def my_groups(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
) -> GroupPageOut:
    groups, next_cursor = await groups_service.my_groups(session, viewer=user, params=page)
    return GroupPageOut(
        items=await views.group_views(
            session, redis, storage, groups=groups, viewer=user, settings=settings
        ),
        next_cursor=next_cursor,
    )


@router.get(
    "/invitations",
    response_model=list[GroupOut],
    summary="Groups the caller is invited to",
    description="Accept or decline each with `POST /v1/groups/{id}/invitation`.",
)
async def my_invitations(
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> list[GroupOut]:
    groups = await groups_service.my_invitations(session, viewer=user)
    return await views.group_views(
        session, redis, storage, groups=groups, viewer=user, settings=settings
    )


@router.get(
    "/invite-links/{token}",
    response_model=GroupOut,
    summary="Preview an invite link",
    description=(
        "The group a link leads to — its profile, never its posts. Every way a link "
        "can be no good (unknown, expired, used up, revoked) is the same 404."
    ),
)
async def preview_invite_link(
    token: str,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GroupOut:
    group = await groups_service.preview_invite_link(
        session, viewer=user, token=token, settings=settings
    )
    return await _one(session, redis, storage, group, user, settings)


@router.post(
    "/invite-links/{token}/join",
    response_model=GroupOut,
    summary="Join through an invite link",
    description=(
        "Straight in, whatever the group's join policy: an admin made the link. "
        "Someone banned from the group gets the same 404 as for a bad link."
    ),
    dependencies=[
        Depends(user_rate_limit(bucket="group_link_join", limit=30, window_seconds=3600))
    ],
)
async def join_with_link(
    token: str,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GroupOut:
    view = await groups_service.join_with_link(session, user=user, token=token, settings=settings)
    return await _one(session, redis, storage, view.group, user, settings)


@router.get(
    "/{group_id}",
    response_model=GroupOut,
    summary="Get a group",
    description=(
        "A public group's profile is visible to anyone signed in; a private group's "
        "to its members and to people invited to it." + NOT_FOUND_DOC
    ),
)
async def get_group(
    group_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GroupOut:
    view = await groups_service.get_group(session, viewer=user, group_id=group_id)
    return await _one(session, redis, storage, view.group, user, settings)


@router.patch(
    "/{group_id}",
    response_model=GroupOut,
    summary="Change a group's settings",
    description=(
        "Admins and the owner: name, description, rules, category, visibility, join "
        "policy, post approval and cover." + NOT_FOUND_DOC
    ),
    dependencies=[Depends(_admin_limit)],
)
async def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdateIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GroupOut:
    group = await groups_service.update_group(
        session,
        actor=user,
        group_id=group_id,
        changes=payload.model_dump(exclude_unset=True),
        settings=settings,
    )
    return await _one(session, redis, storage, group, user, settings)


@router.delete(
    "/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a group",
    description="Owner only. Soft: its posts stay for moderation until retention purges them.",
)
async def delete_group(group_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await groups_service.delete_group(session, actor=user, group_id=group_id)


@router.post(
    "/{group_id}/chat",
    response_model=GroupOut,
    summary="Enable the group's linked chat",
    description=(
        "Admins and the owner. A group chat whose members are exactly the group's "
        "active members, kept in step as people join and leave. Idempotent."
    ),
)
async def enable_chat(
    group_id: uuid.UUID,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GroupOut:
    group = await groups_service.enable_chat(session, actor=user, group_id=group_id)
    return await _one(session, redis, storage, group, user, settings)


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------
@router.post(
    "/{group_id}/join",
    response_model=GroupOut,
    summary="Join, or ask to join, a public group",
    description=(
        "An open group is joined at once; an approval group records a request for "
        "its admins. Private groups are invite-only, and banned users cannot come "
        "back — both are 404s here."
    ),
    dependencies=[Depends(user_rate_limit(bucket="group_join", limit=60, window_seconds=3600))],
)
async def join_group(
    group_id: uuid.UUID,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GroupOut:
    await groups_service.join_group(session, user=user, group_id=group_id, settings=settings)
    view = await groups_service.get_group(session, viewer=user, group_id=group_id)
    await session.refresh(view.group)
    return await _one(session, redis, storage, view.group, user, settings)


@router.post(
    "/{group_id}/leave",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Leave a group, or cancel a request to join",
    description=(
        "An owner who leaves hands the group to its longest-standing admin, then "
        "moderator, then member. A group left with nobody is deleted."
    ),
)
async def leave_group(group_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    await groups_service.leave_group(session, user=user, group_id=group_id)


@router.post(
    "/{group_id}/invitation",
    response_model=GroupOut,
    summary="Accept or decline an invitation",
    description="Declining removes the invitation; the group can invite again.",
)
async def respond_to_invite(
    group_id: uuid.UUID,
    payload: RespondIn,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> GroupOut:
    view: GroupView = await groups_service.respond_to_invite(
        session, user=user, group_id=group_id, accept=payload.action == "accept"
    )
    return await _one(session, redis, storage, view.group, user, settings)


@router.post(
    "/{group_id}/invites",
    response_model=InvitedOut,
    summary="Invite people to a group",
    description=(
        "Admins and the owner. Each invitee accepts or declines. Someone with a "
        "pending request is let in instead. Someone banned is refused with 409 — "
        "unban them first."
    ),
)
async def invite(
    group_id: uuid.UUID,
    payload: InviteIn,
    user: ActiveUser,
    session: SessionDep,
    settings: SettingsDep,
) -> InvitedOut:
    invited = await groups_service.invite(
        session, actor=user, group_id=group_id, user_ids=payload.user_ids, settings=settings
    )
    return InvitedOut(invited=invited)


# ---------------------------------------------------------------------------
# Invite links
# ---------------------------------------------------------------------------
@router.post(
    "/{group_id}/invite-links",
    response_model=InviteLinkOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an invite link",
    description=(
        "Admins and the owner. Every link expires (seven days by default, at most "
        "`GROUP_INVITE_LINK_MAX_DAYS`) and can be limited to a number of uses. The "
        "token is in this response only."
    ),
    dependencies=[Depends(_admin_limit)],
)
async def create_invite_link(
    group_id: uuid.UUID,
    payload: InviteLinkCreateIn,
    user: ActiveUser,
    session: SessionDep,
    settings: SettingsDep,
) -> InviteLinkOut:
    link, token = await groups_service.create_invite_link(
        session,
        actor=user,
        group_id=group_id,
        expires_in_days=payload.expires_in_days,
        max_uses=payload.max_uses,
        settings=settings,
    )
    return InviteLinkOut(
        id=link.id,
        expires_at=link.expires_at,
        max_uses=link.max_uses,
        use_count=link.use_count,
        created_at=link.created_at,
        token=token,
    )


@router.get(
    "/{group_id}/invite-links",
    response_model=list[InviteLinkOut],
    summary="List live invite links",
    description="Admins and the owner. Tokens are not shown: only their hashes are kept.",
)
async def list_invite_links(
    group_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[InviteLinkOut]:
    links = await groups_service.list_invite_links(session, actor=user, group_id=group_id)
    return [
        InviteLinkOut(
            id=link.id,
            expires_at=link.expires_at,
            max_uses=link.max_uses,
            use_count=link.use_count,
            created_at=link.created_at,
        )
        for link in links
    ]


@router.delete(
    "/{group_id}/invite-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an invite link",
    description="Takes effect at once. People who already joined through it stay.",
)
async def revoke_invite_link(
    group_id: uuid.UUID, link_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await groups_service.revoke_invite_link(session, actor=user, group_id=group_id, link_id=link_id)


# ---------------------------------------------------------------------------
# Members and admin tools
# ---------------------------------------------------------------------------
@router.get(
    "/{group_id}/members",
    response_model=MemberPageOut,
    summary="List members, requests, invitations or bans",
    description=(
        "`active` members are listed for anyone who can read the group. `pending` "
        "requests and `invited` people are for admins, and `banned` for admins too. "
        "Anyone blocked either way is left out." + NOT_FOUND_DOC
    ),
)
async def list_members(
    group_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    storage: StorageDep,
    page: PageDep,
    member_status: Annotated[GroupMemberStatus, Query(alias="status")] = GroupMemberStatus.active,
) -> MemberPageOut:
    members, next_cursor = await groups_service.list_members(
        session, viewer=user, group_id=group_id, status=member_status, params=page
    )
    view = await groups_service.get_group(session, viewer=user, group_id=group_id)
    return MemberPageOut(
        items=await views.member_views(session, storage, members=members, viewer_role=view.role),
        next_cursor=next_cursor,
    )


@router.post(
    "/{group_id}/members/{user_id}/review",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Approve or reject a join request",
    description="Admins and the owner. A rejected person may ask again.",
    dependencies=[Depends(_admin_limit)],
)
async def review_join_request(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: ReviewIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    await groups_service.review_join_request(
        session,
        actor=user,
        group_id=group_id,
        user_id=user_id,
        approve=payload.action == "approve",
        settings=settings,
    )


@router.delete(
    "/{group_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove someone from a group",
    description=(
        "Admins and the owner, on people below them. Removed people may come back; "
        "to keep someone out, ban them."
    ),
    dependencies=[Depends(_admin_limit)],
)
async def remove_member(
    group_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await groups_service.remove_member(session, actor=user, group_id=group_id, user_id=user_id)


@router.put(
    "/{group_id}/bans/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Ban someone from a group",
    description=(
        "Admins and the owner, on people below them — members or not. A banned "
        "person loses access at once and cannot return by joining, asking, an "
        "invitation or a link until unbanned. Idempotent."
    ),
    dependencies=[Depends(_admin_limit)],
)
async def ban_member(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: BanIn,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await groups_service.ban_member(
        session, actor=user, group_id=group_id, user_id=user_id, reason=payload.reason
    )


@router.delete(
    "/{group_id}/bans/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Lift a ban",
    description="Admins and the owner. The person may then join again; they are not re-added.",
)
async def unban_member(
    group_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await groups_service.unban_member(session, actor=user, group_id=group_id, user_id=user_id)


@router.put(
    "/{group_id}/members/{user_id}/mute",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mute or unmute a member",
    description=(
        "Moderators and above, on people below them. A muted member can read but "
        "not post, comment or react, until the time given (at most a year)."
    ),
    dependencies=[Depends(_admin_limit)],
)
async def mute_member(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MuteIn,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await groups_service.mute_member(
        session, actor=user, group_id=group_id, user_id=user_id, until=payload.until
    )


@router.put(
    "/{group_id}/members/{user_id}/role",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change a member's role",
    description=(
        "Admins appoint and remove moderators. Only the owner makes or unmakes "
        "admins, and only the owner can hand the group over by giving `owner`."
    ),
    dependencies=[Depends(_admin_limit)],
)
async def set_role(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: RoleIn,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await groups_service.set_role(
        session, actor=user, group_id=group_id, user_id=user_id, role=payload.role
    )


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------
@router.post(
    "/{group_id}/posts",
    response_model=PostOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post in a group",
    description=(
        "Active, unmuted members. In a group that approves posts, a member's post "
        "waits as `pending` until a moderator reviews it; moderators' own posts do "
        "not. Group posts never appear in the neighbourhood feed. Shares the daily "
        f"post limit with the feed.\n\nSend an `{IDEMPOTENCY_HEADER}` header to make "
        "retries safe." + NOT_FOUND_DOC
    ),
    dependencies=[Depends(_post_limit)],
)
async def create_group_post(
    group_id: uuid.UUID,
    payload: PostCreateIn,
    request: Request,
    user: ActiveUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> JSONResponse:
    async def action() -> PostOut:
        post = await groups_service.create_group_post(
            session,
            redis,
            author=user,
            group_id=group_id,
            body=payload.body,
            category=payload.category,
            media_ids=payload.media_ids,
            settings=settings,
        )
        (rendered,) = await _posts(session, redis, storage, [post], user, settings)
        return rendered

    return await execute_idempotently(
        session,
        request=request,
        user_id=user.id,
        payload={"group_id": str(group_id), **payload.model_dump(mode="json")},
        status_code=status.HTTP_201_CREATED,
        action=action,
        settings=settings,
    )


@router.get(
    "/{group_id}/posts",
    response_model=GroupPostPageOut,
    summary="A group's posts",
    description=(
        "Newest first, with pinned posts separately on the first page. Approved "
        "posts, plus the caller's own pending ones. Comments, reactions and reports "
        "use the ordinary post endpoints." + NOT_FOUND_DOC
    ),
)
async def list_group_posts(
    group_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
) -> GroupPostPageOut:
    result = await groups_service.list_group_posts(
        session, viewer=user, group_id=group_id, params=page
    )
    return GroupPostPageOut(
        items=await _posts(session, redis, storage, result.posts, user, settings),
        pinned=await _posts(session, redis, storage, result.pinned, user, settings),
        next_cursor=result.next_cursor,
    )


@router.get(
    "/{group_id}/posts/pending",
    response_model=GroupPostPageOut,
    summary="Posts waiting for approval",
    description="Moderators and above. Oldest first, so the queue is worked in order.",
)
async def pending_posts(
    group_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
    page: PageDep,
) -> GroupPostPageOut:
    posts, next_cursor = await groups_service.pending_posts(
        session, viewer=user, group_id=group_id, params=page
    )
    return GroupPostPageOut(
        items=await _posts(session, redis, storage, posts, user, settings),
        next_cursor=next_cursor,
    )


@router.post(
    "/{group_id}/posts/{post_id}/review",
    response_model=PostOut,
    summary="Approve or reject a pending post",
    description="Moderators and above. The author is notified of an approval.",
    dependencies=[Depends(_admin_limit)],
)
async def review_post(
    group_id: uuid.UUID,
    post_id: uuid.UUID,
    payload: ReviewIn,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> PostOut:
    post = await groups_service.review_post(
        session,
        actor=user,
        group_id=group_id,
        post_id=post_id,
        approve=payload.action == "approve",
        settings=settings,
    )
    (rendered,) = await _posts(session, redis, storage, [post], user, settings)
    return rendered


@router.put(
    "/{group_id}/posts/{post_id}/pin",
    response_model=PostOut,
    summary="Pin or unpin a post",
    description=f"Admins and the owner. At most {groups_service.MAX_PINNED_POSTS} pinned at once.",
    dependencies=[Depends(_admin_limit)],
)
async def set_pinned(
    group_id: uuid.UUID,
    post_id: uuid.UUID,
    payload: PinIn,
    user: CurrentUser,
    session: SessionDep,
    redis: RedisDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> PostOut:
    post = await groups_service.set_pinned(
        session, actor=user, group_id=group_id, post_id=post_id, pinned=payload.pinned
    )
    (rendered,) = await _posts(session, redis, storage, [post], user, settings)
    return rendered


@router.delete(
    "/{group_id}/posts/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a post from the group",
    description=(
        "Moderators and above, on anyone's post. Authors delete their own with "
        "`DELETE /v1/posts/{id}`. Recorded in the audit log."
    ),
    dependencies=[Depends(_admin_limit)],
)
async def remove_post(
    group_id: uuid.UUID, post_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await groups_service.remove_post(session, actor=user, group_id=group_id, post_id=post_id)
