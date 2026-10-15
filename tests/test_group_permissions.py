"""The Phase 4 acceptance tests: who can see and do what in a group.

The brief's "done when" is three properties:

1. non-members cannot read private groups;
2. members cannot use admin tools;
3. banned users cannot rejoin.

The admin-tools test is a matrix. Every admin endpoint is called by every role,
and the expected answer comes from `groups.permissions.MINIMUM_ROLE` — the same
table the code enforces. A refusal is a 403; the matrix asserts exactly when one
occurs, not just that something failed.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.feed.models import Post
from app.modules.groups import service as groups_service
from app.modules.groups.models import (
    ROLE_RANK,
    Group,
    GroupJoinPolicy,
    GroupRole,
    GroupVisibility,
)
from app.modules.groups.permissions import MINIMUM_ROLE, GroupAction
from app.modules.users.models import User
from tests.factories import NYERI_TOWN, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


@dataclass
class Cast:
    """A group with one person in each role, plus a plain member to act on."""

    group_id: uuid.UUID
    people: dict[GroupRole, User]
    target: User
    requester: User
    post_id: uuid.UUID
    pending_post_id: uuid.UUID

    def ids(self) -> dict[str, str]:
        return {
            "group": str(self.group_id),
            "target": str(self.target.id),
            "requester": str(self.requester.id),
            "post": str(self.post_id),
            "pending": str(self.pending_post_id),
        }


async def make_cast(
    db_session: AsyncSession,
    redis: Redis,
    *,
    visibility: GroupVisibility = GroupVisibility.public,
) -> Cast:
    settings = get_settings()
    owner = await make_user(db_session, at=NYERI_TOWN, created_days_ago=30)
    group = await groups_service.create_group(
        db_session,
        creator=owner,
        name="Kamakwa Estate Residents",
        description="Our estate",
        category="estate",
        visibility=visibility,
        join_policy=GroupJoinPolicy.approval,
        requires_post_approval=True,
        rules=None,
        cover_media_id=None,
        settings=settings,
    )
    people = {GroupRole.owner: owner}
    for role in (GroupRole.admin, GroupRole.moderator, GroupRole.member):
        person = await make_user(db_session, at=NYERI_TOWN, created_days_ago=30)
        await groups_service.invite(
            db_session, actor=owner, group_id=group.id, user_ids=[person.id], settings=settings
        )
        await groups_service.respond_to_invite(
            db_session, user=person, group_id=group.id, accept=True
        )
        if role != GroupRole.member:
            await groups_service.set_role(
                db_session, actor=owner, group_id=group.id, user_id=person.id, role=role
            )
        people[role] = person
    target = await make_user(db_session, at=NYERI_TOWN, created_days_ago=30)
    await groups_service.invite(
        db_session, actor=owner, group_id=group.id, user_ids=[target.id], settings=settings
    )
    await groups_service.respond_to_invite(db_session, user=target, group_id=group.id, accept=True)

    requester = await make_user(db_session, at=NYERI_TOWN, created_days_ago=30)
    if visibility == GroupVisibility.public:
        await groups_service.join_group(
            db_session, user=requester, group_id=group.id, settings=settings
        )

    post = await groups_service.create_group_post(
        db_session,
        redis,
        author=owner,
        group_id=group.id,
        body="Water will be off on Saturday",
        category="general",
        media_ids=[],
        settings=settings,
    )
    pending = await groups_service.create_group_post(
        db_session,
        redis,
        author=target,
        group_id=group.id,
        body="Selling a sofa",
        category="general",
        media_ids=[],
        settings=settings,
    )
    await db_session.flush()
    return Cast(
        group_id=group.id,
        people=people,
        target=target,
        requester=requester,
        post_id=post.id,
        pending_post_id=pending.id,
    )


FUTURE = (datetime.now(UTC) + timedelta(days=3)).isoformat()

# (action, method, path, body). Paths use {group}, {target}, {requester}, {post},
# {pending}. Every admin endpoint the router exposes is here.
ADMIN_ENDPOINTS: list[tuple[GroupAction, str, str, dict[str, Any] | None]] = [
    (GroupAction.edit_settings, "PATCH", "/v1/groups/{group}", {"description": "New"}),
    (GroupAction.enable_chat, "POST", "/v1/groups/{group}/chat", None),
    (GroupAction.invite, "POST", "/v1/groups/{group}/invites", {"user_ids": ["{requester}"]}),
    (GroupAction.manage_invite_links, "POST", "/v1/groups/{group}/invite-links", {}),
    (GroupAction.manage_invite_links, "GET", "/v1/groups/{group}/invite-links", None),
    (GroupAction.review_join_requests, "GET", "/v1/groups/{group}/members?status=pending", None),
    (GroupAction.ban_members, "GET", "/v1/groups/{group}/members?status=banned", None),
    (
        GroupAction.review_join_requests,
        "POST",
        "/v1/groups/{group}/members/{requester}/review",
        {"action": "approve"},
    ),
    (GroupAction.remove_members, "DELETE", "/v1/groups/{group}/members/{target}", None),
    (GroupAction.ban_members, "PUT", "/v1/groups/{group}/bans/{target}", {}),
    (GroupAction.ban_members, "DELETE", "/v1/groups/{group}/bans/{target}", None),
    (
        GroupAction.mute_members,
        "PUT",
        "/v1/groups/{group}/members/{target}/mute",
        {"until": FUTURE},
    ),
    (
        GroupAction.appoint_moderators,
        "PUT",
        "/v1/groups/{group}/members/{target}/role",
        {"role": "moderator"},
    ),
    (
        GroupAction.appoint_admins,
        "PUT",
        "/v1/groups/{group}/members/{target}/role",
        {"role": "admin"},
    ),
    (
        GroupAction.transfer_ownership,
        "PUT",
        "/v1/groups/{group}/members/{target}/role",
        {"role": "owner"},
    ),
    (GroupAction.review_posts, "GET", "/v1/groups/{group}/posts/pending", None),
    (
        GroupAction.review_posts,
        "POST",
        "/v1/groups/{group}/posts/{pending}/review",
        {"action": "approve"},
    ),
    (GroupAction.pin_posts, "PUT", "/v1/groups/{group}/posts/{post}/pin", {"pinned": True}),
    (GroupAction.remove_posts, "DELETE", "/v1/groups/{group}/posts/{post}", None),
    (GroupAction.delete_group, "DELETE", "/v1/groups/{group}", None),
]


def _fill(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, list):
        return [_fill(item, ids) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, ids) for key, item in value.items()}
    return value


def test_every_admin_action_is_exercised() -> None:
    """A new action added to the table must get an endpoint row here."""
    covered = {action for action, *_ in ADMIN_ENDPOINTS}
    assert covered == set(GroupAction) - {GroupAction.participate}


@pytest.mark.parametrize("role", list(GroupRole), ids=lambda role: role.value)
@pytest.mark.parametrize(
    ("action", "method", "path", "body"),
    ADMIN_ENDPOINTS,
    ids=[
        f"{action.value}:{method}:{path.rsplit('/', 1)[-1]}"
        for action, method, path, _ in ADMIN_ENDPOINTS
    ],
)
async def test_admin_tools_follow_the_permission_table(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    role: GroupRole,
    action: GroupAction,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    cast = await make_cast(db_session, redis_client)
    ids = cast.ids()
    client = await authenticate(cast.people[role])

    response = await client.request(
        method, _fill(path, ids), json=_fill(body, ids) if body is not None else None
    )

    allowed = ROLE_RANK[role] >= ROLE_RANK[MINIMUM_ROLE[action]]
    if allowed:
        assert response.status_code != 403, response.text
        assert response.status_code < 500, response.text
    else:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["details"]["action"] == action.value


async def test_nobody_acts_on_an_equal_or_higher_rank(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    admin, moderator = cast.people[GroupRole.admin], cast.people[GroupRole.moderator]
    owner_id, other_admin = cast.people[GroupRole.owner].id, cast.people[GroupRole.admin].id
    moderator_id = moderator.id
    group = str(cast.group_id)

    admin_client = await authenticate(admin)
    assert (await admin_client.delete(f"/v1/groups/{group}/members/{owner_id}")).status_code == 403
    assert (
        await admin_client.put(f"/v1/groups/{group}/bans/{owner_id}", json={})
    ).status_code == 403

    # A second admin, to test admin-on-admin.
    other = await make_user(db_session, at=NYERI_TOWN)
    settings = get_settings()
    owner = cast.people[GroupRole.owner]
    await db_session.refresh(owner)
    await groups_service.invite(
        db_session, actor=owner, group_id=cast.group_id, user_ids=[other.id], settings=settings
    )
    await groups_service.respond_to_invite(
        db_session, user=other, group_id=cast.group_id, accept=True
    )
    await groups_service.set_role(
        db_session, actor=owner, group_id=cast.group_id, user_id=other.id, role=GroupRole.admin
    )
    other_id = other.id
    admin_client = await authenticate(admin)
    assert (await admin_client.delete(f"/v1/groups/{group}/members/{other_id}")).status_code == 403
    assert other_admin != other_id

    moderator_client = await authenticate(moderator)
    response = await moderator_client.put(
        f"/v1/groups/{group}/members/{moderator_id}/mute", json={"until": FUTURE}
    )
    assert response.status_code in (400, 403)


# ---------------------------------------------------------------------------
# 1. Non-members cannot read private groups
# ---------------------------------------------------------------------------
async def test_a_non_member_cannot_read_a_private_group(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client, visibility=GroupVisibility.private)
    outsider = await make_user(db_session, at=NYERI_TOWN)
    group, post = str(cast.group_id), str(cast.post_id)
    client = await authenticate(outsider)

    reads = [
        f"/v1/groups/{group}",
        f"/v1/groups/{group}/posts",
        f"/v1/groups/{group}/members",
        f"/v1/posts/{post}",
        f"/v1/posts/{post}/comments",
    ]
    for path in reads:
        response = await client.get(path)
        assert response.status_code == 404, (path, response.status_code)

    # Nor through any write that would reveal it exists.
    for method, path, body in (
        ("POST", f"/v1/groups/{group}/join", None),
        ("POST", f"/v1/groups/{group}/posts", {"body": "hi", "category": "general"}),
        ("POST", f"/v1/posts/{post}/comments", {"body": "hi"}),
        ("PUT", f"/v1/posts/{post}/reaction", {"kind": "like"}),
        ("POST", "/v1/reports", {"target_type": "post", "target_id": post, "reason": "spam"}),
        ("POST", "/v1/reports", {"target_type": "group", "target_id": group, "reason": "spam"}),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code == 404, (method, path, response.status_code)


async def test_an_invitee_sees_the_profile_but_not_the_posts(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client, visibility=GroupVisibility.private)
    invitee = await make_user(db_session, at=NYERI_TOWN)
    owner = cast.people[GroupRole.owner]
    await db_session.refresh(owner)
    await groups_service.invite(
        db_session,
        actor=owner,
        group_id=cast.group_id,
        user_ids=[invitee.id],
        settings=get_settings(),
    )
    client = await authenticate(invitee)

    profile = await client.get(f"/v1/groups/{cast.group_id}")
    assert profile.status_code == 200
    assert profile.json()["my_membership"]["status"] == "invited"
    assert profile.json()["area_label"] is None
    assert (await client.get(f"/v1/groups/{cast.group_id}/posts")).status_code == 404
    assert (await client.get(f"/v1/posts/{cast.post_id}")).status_code == 404


async def test_a_private_group_is_never_discovered(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    await make_cast(db_session, redis_client, visibility=GroupVisibility.private)
    neighbour = await make_user(db_session, at=NYERI_TOWN)

    found = (await (await authenticate(neighbour)).get("/v1/groups/discover?q=kamakwa")).json()

    assert found["items"] == []


async def test_a_private_group_stores_no_location(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    cast = await make_cast(db_session, redis_client, visibility=GroupVisibility.private)
    group = await db_session.get(Group, cast.group_id)
    assert group is not None
    assert (group.centroid, group.cell_res9, group.cell_res8) == (None, None, None)


async def test_members_of_a_private_group_can_read_it(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client, visibility=GroupVisibility.private)
    client = await authenticate(cast.people[GroupRole.member])

    posts = (await client.get(f"/v1/groups/{cast.group_id}/posts")).json()

    assert [item["body"] for item in posts["items"]] == ["Water will be off on Saturday"]
    assert (await client.get(f"/v1/posts/{cast.post_id}")).status_code == 200


async def test_a_public_group_is_readable_but_not_writable_by_outsiders(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    outsider = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(outsider)

    assert (await client.get(f"/v1/groups/{cast.group_id}/posts")).status_code == 200
    assert (await client.get(f"/v1/posts/{cast.post_id}")).status_code == 200
    for method, path, body in (
        ("POST", f"/v1/groups/{cast.group_id}/posts", {"body": "hi", "category": "general"}),
        ("POST", f"/v1/posts/{cast.post_id}/comments", {"body": "hi"}),
        ("PUT", f"/v1/posts/{cast.post_id}/reaction", {"kind": "like"}),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.status_code)


# ---------------------------------------------------------------------------
# 3. Banned users cannot rejoin
# ---------------------------------------------------------------------------
async def _ban(cast: Cast, authenticate: Authenticate, user_id: uuid.UUID) -> None:
    owner_client = await authenticate(cast.people[GroupRole.owner])
    response = await owner_client.put(
        f"/v1/groups/{cast.group_id}/bans/{user_id}", json={"reason": "Scam posts"}
    )
    assert response.status_code == 204, response.text


async def test_a_banned_member_loses_access_at_once(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    member = cast.people[GroupRole.member]
    member_id = member.id

    await _ban(cast, authenticate, member_id)

    client = await authenticate(member)
    for path in (
        f"/v1/groups/{cast.group_id}",
        f"/v1/groups/{cast.group_id}/posts",
        f"/v1/posts/{cast.post_id}",
    ):
        assert (await client.get(path)).status_code == 404, path
    group = await db_session.get(Group, cast.group_id)
    assert group is not None
    await db_session.refresh(group)
    # Owner, admin, moderator and the target remain; the requester is only pending.
    assert group.member_count == 4


async def test_a_banned_user_cannot_rejoin_by_any_route(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    member = cast.people[GroupRole.member]
    member_id = member.id
    owner_client = await authenticate(cast.people[GroupRole.owner])
    link = (await owner_client.post(f"/v1/groups/{cast.group_id}/invite-links", json={})).json()
    await _ban(cast, authenticate, member_id)

    client = await authenticate(member)
    # Joining, and asking to join.
    assert (await client.post(f"/v1/groups/{cast.group_id}/join")).status_code == 404
    # An invite link.
    assert (await client.get(f"/v1/groups/invite-links/{link['token']}")).status_code == 404
    assert (await client.post(f"/v1/groups/invite-links/{link['token']}/join")).status_code == 404
    # Accepting an invitation.
    assert (
        await client.post(f"/v1/groups/{cast.group_id}/invitation", json={"action": "accept"})
    ).status_code == 404
    # Leaving does not launder the ban.
    assert (await client.post(f"/v1/groups/{cast.group_id}/leave")).status_code == 404
    # And an admin's invitation is refused rather than quietly lifting it.
    owner_client = await authenticate(cast.people[GroupRole.owner])
    invited = await owner_client.post(
        f"/v1/groups/{cast.group_id}/invites", json={"user_ids": [str(member_id)]}
    )
    assert invited.status_code == 409

    client = await authenticate(member)
    assert (await client.post(f"/v1/groups/{cast.group_id}/join")).status_code == 404


async def test_a_ban_can_be_placed_on_someone_not_yet_a_member(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    stranger = await make_user(db_session, at=NYERI_TOWN)
    stranger_id = stranger.id

    await _ban(cast, authenticate, stranger_id)

    assert (
        await (await authenticate(stranger)).post(f"/v1/groups/{cast.group_id}/join")
    ).status_code == 404


async def test_banned_users_do_not_see_the_group_in_discovery(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    member = cast.people[GroupRole.member]
    await _ban(cast, authenticate, member.id)

    found = (await (await authenticate(member)).get("/v1/groups/discover?q=kamakwa")).json()

    assert found["items"] == []


async def test_unbanning_lets_them_join_again(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    member = cast.people[GroupRole.member]
    member_id = member.id
    await _ban(cast, authenticate, member_id)

    owner_client = await authenticate(cast.people[GroupRole.owner])
    assert (
        await owner_client.delete(f"/v1/groups/{cast.group_id}/bans/{member_id}")
    ).status_code == 204

    joined = await (await authenticate(member)).post(f"/v1/groups/{cast.group_id}/join")
    assert joined.status_code == 200
    # An approval group: they are asking again, not let straight back in.
    assert joined.json()["my_membership"]["status"] == "pending"


async def test_a_ban_survives_in_the_member_list_for_admins(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    member = cast.people[GroupRole.member]
    member_id = str(member.id)
    await _ban(cast, authenticate, member.id)

    banned = (
        await (await authenticate(cast.people[GroupRole.admin])).get(
            f"/v1/groups/{cast.group_id}/members?status=banned"
        )
    ).json()["items"]

    assert [(item["user"]["id"], item["ban_reason"]) for item in banned] == [
        (member_id, "Scam posts")
    ]


async def test_group_posts_never_reach_the_feed(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    cast = await make_cast(db_session, redis_client)
    neighbour = await make_user(db_session, at=NYERI_TOWN)

    feed = (await (await authenticate(neighbour)).get("/v1/feed")).json()

    assert all(item["id"] != str(cast.post_id) for item in feed["items"])
    assert await db_session.get(Post, cast.post_id) is not None
