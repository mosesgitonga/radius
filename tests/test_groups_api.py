"""Groups end to end: creating, discovering, joining, links, posts, chat, data rights."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.providers.storage import MemoryStorageProvider
from app.modules.groups.models import Group, GroupInviteLink, GroupMember
from app.modules.media.models import MediaPurpose
from app.modules.messaging.models import ConversationMember, MemberStatus
from app.modules.moderation.models import AuditAction, AuditEvent
from app.modules.notifications.models import Notification
from app.modules.users.models import User
from app.modules.users.service import block_user
from tests.factories import KARATINA, NAIROBI, NYERI_TOWN, make_ready_image, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def create(
    client: httpx.AsyncClient,
    *,
    name: str = "Kamakwa Estate Residents",
    visibility: str = "public",
    join_policy: str = "open",
    requires_post_approval: bool = False,
    category: str = "estate",
    **extra: Any,
) -> dict[str, Any]:
    response = await client.post(
        "/v1/groups",
        json={
            "name": name,
            "description": "For everyone on the estate",
            "category": category,
            "visibility": visibility,
            "join_policy": join_policy,
            "requires_post_approval": requires_post_approval,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def neighbour(db_session: AsyncSession, **kwargs: Any) -> User:
    return await make_user(
        db_session, at=kwargs.pop("at", NYERI_TOWN), created_days_ago=30, **kwargs
    )


async def join(authenticate: Authenticate, person: User, group_id: str) -> dict[str, Any]:
    response = await (await authenticate(person)).post(f"/v1/groups/{group_id}/join")
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------
async def test_a_public_group_is_placed_at_its_creators_home(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))

    assert group["member_count"] == 1
    assert group["my_membership"]["role"] == "owner"
    assert group["area_label"] is not None
    assert group["distance_bucket"] is None or group["distance_bucket"] == "under_1km"
    stored = await db_session.get(Group, uuid.UUID(group["id"]))
    assert stored is not None
    assert stored.cell_res9 == owner.home_cell_res9
    # Only labels and bands leave the server.
    assert not {"centroid", "cell_res9", "cell_res8", "latitude"} & set(group)


async def test_a_public_group_needs_a_home_area(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    homeless = await make_user(db_session, created_days_ago=30)
    response = await (await authenticate(homeless)).post(
        "/v1/groups",
        json={"name": "X", "category": "estate", "visibility": "public"},
    )
    assert response.status_code == 409


async def test_categories_come_from_configuration(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    response = await (await authenticate(owner)).post(
        "/v1/groups", json={"name": "X", "category": "nightclub", "visibility": "private"}
    )
    assert response.status_code == 400
    assert "estate" in response.json()["error"]["details"]["categories"]


async def test_new_accounts_can_create_one_group_a_day(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    newcomer = await make_user(db_session, at=NYERI_TOWN, created_days_ago=0)
    client = await authenticate(newcomer)
    await create(client, visibility="private")

    second = await client.post(
        "/v1/groups", json={"name": "Again", "category": "other", "visibility": "private"}
    )

    assert second.status_code == 429


async def test_a_cover_image_must_be_a_group_upload(
    db_session: AsyncSession, authenticate: Authenticate, storage: MemoryStorageProvider
) -> None:
    owner = await neighbour(db_session)
    cover = await make_ready_image(db_session, storage, owner=owner, purpose=MediaPurpose.group)
    wrong = await make_ready_image(db_session, storage, owner=owner, purpose=MediaPurpose.post)
    client = await authenticate(owner)

    group = await create(client, cover_media_id=str(cover.id))
    refused = await client.post(
        "/v1/groups",
        json={
            "name": "Y",
            "category": "other",
            "visibility": "private",
            "cover_media_id": str(wrong.id),
        },
    )

    assert set(group["cover"]) >= {"320", "720"}
    assert refused.status_code == 400


async def test_changing_visibility_moves_or_forgets_the_place(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session, at=KARATINA)
    client = await authenticate(owner)
    group = await create(client, visibility="private")
    stored = await db_session.get(Group, uuid.UUID(group["id"]))
    assert stored is not None and stored.centroid is None

    public = await client.patch(f"/v1/groups/{group['id']}", json={"visibility": "public"})
    assert public.status_code == 200
    await db_session.refresh(stored)
    assert stored.cell_res9 == owner.home_cell_res9

    private = await client.patch(f"/v1/groups/{group['id']}", json={"visibility": "private"})
    assert private.json()["area_label"] is None
    await db_session.refresh(stored)
    assert stored.cell_res9 is None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
async def test_discovery_finds_nearby_public_groups_only(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    near = await create(await authenticate(await neighbour(db_session)), name="Kamakwa Estate")
    await create(
        await authenticate(await neighbour(db_session)), name="Secret chama", visibility="private"
    )
    await create(
        await authenticate(await neighbour(db_session, at=NAIROBI)), name="Kileleshwa Residents"
    )

    found = (
        await (await authenticate(await neighbour(db_session))).get("/v1/groups/discover")
    ).json()

    assert [item["id"] for item in found["items"]] == [near["id"]]
    assert found["radius_km"] >= 2


async def test_search_tolerates_typos_and_partial_names(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    group = await create(await authenticate(await neighbour(db_session)))
    await create(
        await authenticate(await neighbour(db_session)), name="Nyeri Hill Church", category="church"
    )
    client = await authenticate(await neighbour(db_session))

    for query in ("kamakwa", "Kamkwa", "estate residents"):
        found = (await client.get("/v1/groups/discover", params={"q": query})).json()
        assert [item["id"] for item in found["items"]] == [group["id"]], query
    church = (await client.get("/v1/groups/discover", params={"category": "church"})).json()
    assert [item["name"] for item in church["items"]] == ["Nyeri Hill Church"]


async def test_the_busiest_groups_come_first(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    quiet = await create(await authenticate(await neighbour(db_session)), name="Quiet group")
    busy = await create(await authenticate(await neighbour(db_session)), name="Busy group")
    for _ in range(2):
        await join(authenticate, await neighbour(db_session), busy["id"])

    found = (
        await (await authenticate(await neighbour(db_session))).get("/v1/groups/discover")
    ).json()

    assert [item["id"] for item in found["items"]] == [busy["id"], quiet["id"]]


async def test_a_distance_filter_cannot_exceed_its_cap(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    far = await create(await authenticate(await neighbour(db_session, at=NAIROBI)), name="Far")
    client = await authenticate(await neighbour(db_session))

    found = (await client.get("/v1/groups/discover", params={"distance_km": 100})).json()

    # Nairobi is well over 25 km from Nyeri, the groups filter cap.
    assert found["radius_km"] <= 25
    assert far["id"] not in [item["id"] for item in found["items"]]


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------
async def test_joining_an_open_group(db_session: AsyncSession, authenticate: Authenticate) -> None:
    group = await create(await authenticate(await neighbour(db_session)))

    joined = await join(authenticate, await neighbour(db_session), group["id"])

    assert joined["my_membership"]["status"] == "active"
    assert joined["member_count"] == 2


async def test_asking_to_join_and_being_approved(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), join_policy="approval")
    applicant = await neighbour(db_session)
    applicant_id = applicant.id

    asked = await join(authenticate, applicant, group["id"])
    assert asked["my_membership"]["status"] == "pending"
    assert asked["member_count"] == 1
    notified = (
        await db_session.execute(select(Notification).where(Notification.user_id == owner.id))
    ).scalar_one()
    assert notified.type == "group_join_request"

    owner_client = await authenticate(owner)
    pending = (await owner_client.get(f"/v1/groups/{group['id']}/members?status=pending")).json()
    assert [item["user"]["id"] for item in pending["items"]] == [str(applicant_id)]
    approved = await owner_client.post(
        f"/v1/groups/{group['id']}/members/{applicant_id}/review", json={"action": "approve"}
    )
    assert approved.status_code == 204

    view = (await (await authenticate(applicant)).get(f"/v1/groups/{group['id']}")).json()
    assert view["my_membership"]["status"] == "active"
    kinds = (
        (
            await db_session.execute(
                select(Notification.type).where(Notification.user_id == applicant_id)
            )
        )
        .scalars()
        .all()
    )
    assert list(kinds) == ["group_join_approved"]


async def test_a_rejected_applicant_may_ask_again(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), join_policy="approval")
    applicant = await neighbour(db_session)
    applicant_id = applicant.id
    await join(authenticate, applicant, group["id"])

    await (await authenticate(owner)).post(
        f"/v1/groups/{group['id']}/members/{applicant_id}/review", json={"action": "reject"}
    )

    again = await join(authenticate, applicant, group["id"])
    assert again["my_membership"]["status"] == "pending"


async def test_the_last_person_leaving_deletes_the_group(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    client = await authenticate(owner)
    group = await create(client)

    assert (await client.post(f"/v1/groups/{group['id']}/leave")).status_code == 204

    assert (await client.get(f"/v1/groups/{group['id']}")).status_code == 404


async def test_an_owner_leaving_hands_the_group_to_an_admin(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    early, admin = await neighbour(db_session), await neighbour(db_session)
    await join(authenticate, early, group["id"])
    await join(authenticate, admin, group["id"])
    admin_id = admin.id
    owner_client = await authenticate(owner)
    await owner_client.put(
        f"/v1/groups/{group['id']}/members/{admin_id}/role", json={"role": "admin"}
    )

    await owner_client.post(f"/v1/groups/{group['id']}/leave")

    view = (await (await authenticate(admin)).get(f"/v1/groups/{group['id']}")).json()
    assert view["my_membership"]["role"] == "owner"
    stored = await db_session.get(Group, uuid.UUID(group["id"]))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.owner_id == admin_id


async def test_handing_over_ownership(db_session: AsyncSession, authenticate: Authenticate) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    successor = await neighbour(db_session)
    await join(authenticate, successor, group["id"])
    successor_id = successor.id

    response = await (await authenticate(owner)).put(
        f"/v1/groups/{group['id']}/members/{successor_id}/role", json={"role": "owner"}
    )

    assert response.status_code == 204
    mine = (await (await authenticate(owner)).get(f"/v1/groups/{group['id']}")).json()
    assert mine["my_membership"]["role"] == "admin"
    audit = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.group_ownership_transferred)
        )
    ).scalar_one()
    assert audit.actor_id == owner.id


# ---------------------------------------------------------------------------
# Invitations and links
# ---------------------------------------------------------------------------
async def test_a_direct_invitation(db_session: AsyncSession, authenticate: Authenticate) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), visibility="private")
    invitee = await neighbour(db_session)
    invitee_id = invitee.id

    invited = await (await authenticate(owner)).post(
        f"/v1/groups/{group['id']}/invites", json={"user_ids": [str(invitee_id)]}
    )
    assert invited.json() == {"invited": [str(invitee_id)]}
    client = await authenticate(invitee)
    assert [item["id"] for item in (await client.get("/v1/groups/invitations")).json()] == [
        group["id"]
    ]

    accepted = await client.post(f"/v1/groups/{group['id']}/invitation", json={"action": "accept"})

    assert accepted.json()["my_membership"]["status"] == "active"
    assert accepted.json()["member_count"] == 2


async def test_inviting_someone_blocked_is_a_404(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), visibility="private")
    blocker = await neighbour(db_session)
    await block_user(db_session, blocker=blocker, blocked_id=owner.id)

    response = await (await authenticate(owner)).post(
        f"/v1/groups/{group['id']}/invites", json={"user_ids": [str(blocker.id)]}
    )

    assert response.status_code == 404


async def test_an_invite_link_lets_people_into_a_private_group(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), visibility="private")
    link = (
        await (await authenticate(owner)).post(f"/v1/groups/{group['id']}/invite-links", json={})
    ).json()
    assert link["token"]
    stored = (await db_session.execute(select(GroupInviteLink))).scalar_one()
    assert stored.token_hash != link["token"]
    assert stored.expires_at - datetime.now(UTC) <= timedelta(
        days=settings.group_invite_link_default_days
    )

    guest = await neighbour(db_session)
    client = await authenticate(guest)
    preview = await client.get(f"/v1/groups/invite-links/{link['token']}")
    assert preview.json()["name"] == "Kamakwa Estate Residents"
    joined = await client.post(f"/v1/groups/invite-links/{link['token']}/join")

    assert joined.status_code == 200
    assert joined.json()["my_membership"]["status"] == "active"
    listed = (
        await (await authenticate(owner)).get(f"/v1/groups/{group['id']}/invite-links")
    ).json()
    assert [(item["use_count"], item["token"]) for item in listed] == [(1, None)]


async def test_a_link_bypasses_join_approval(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), join_policy="approval")
    link = (
        await (await authenticate(owner)).post(f"/v1/groups/{group['id']}/invite-links", json={})
    ).json()

    joined = await (await authenticate(await neighbour(db_session))).post(
        f"/v1/groups/invite-links/{link['token']}/join"
    )

    assert joined.json()["my_membership"]["status"] == "active"


@pytest.mark.parametrize("problem", ["used_up", "expired", "revoked", "unknown"])
async def test_a_link_that_is_no_good_is_a_404(
    db_session: AsyncSession, authenticate: Authenticate, problem: str
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), visibility="private")
    owner_client = await authenticate(owner)
    link = (
        await owner_client.post(f"/v1/groups/{group['id']}/invite-links", json={"max_uses": 1})
    ).json()
    token = link["token"]
    if problem == "used_up":
        await (await authenticate(await neighbour(db_session))).post(
            f"/v1/groups/invite-links/{token}/join"
        )
    elif problem == "expired":
        await db_session.execute(
            update(GroupInviteLink).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await db_session.commit()
    elif problem == "revoked":
        await (await authenticate(owner)).delete(
            f"/v1/groups/{group['id']}/invite-links/{link['id']}"
        )
    else:
        token = "not-a-real-token"

    client = await authenticate(await neighbour(db_session))
    assert (await client.get(f"/v1/groups/invite-links/{token}")).status_code == 404
    assert (await client.post(f"/v1/groups/invite-links/{token}/join")).status_code == 404


async def test_links_cannot_last_for_ever(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), visibility="private")

    response = await (await authenticate(owner)).post(
        f"/v1/groups/{group['id']}/invite-links",
        json={"expires_in_days": settings.group_invite_link_max_days + 1},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------
async def test_posting_and_reading_in_a_group(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    member = await neighbour(db_session)
    await join(authenticate, member, group["id"])

    posted = await (await authenticate(member)).post(
        f"/v1/groups/{group['id']}/posts",
        json={"body": "Mama mboga is here", "category": "general"},
    )

    assert posted.status_code == 201
    assert posted.json()["group_id"] == group["id"]
    assert posted.json()["approval_status"] is None
    posts = (await (await authenticate(owner)).get(f"/v1/groups/{group['id']}/posts")).json()
    assert [item["body"] for item in posts["items"]] == ["Mama mboga is here"]
    # Comments go through the ordinary endpoint, for members.
    comment = await (await authenticate(owner)).post(
        f"/v1/posts/{posted.json()['id']}/comments", json={"body": "Thanks"}
    )
    assert comment.status_code == 201


async def test_posts_awaiting_approval(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), requires_post_approval=True)
    member, other = await neighbour(db_session), await neighbour(db_session)
    await join(authenticate, member, group["id"])
    await join(authenticate, other, group["id"])

    pending = (
        await (await authenticate(member)).post(
            f"/v1/groups/{group['id']}/posts",
            json={"body": "For sale: sofa", "category": "general"},
        )
    ).json()
    assert pending["approval_status"] == "pending"

    # The author sees it; another member does not, by list or by ID.
    mine = (await (await authenticate(member)).get(f"/v1/groups/{group['id']}/posts")).json()
    assert [item["id"] for item in mine["items"]] == [pending["id"]]
    other_client = await authenticate(other)
    assert (await other_client.get(f"/v1/groups/{group['id']}/posts")).json()["items"] == []
    assert (await other_client.get(f"/v1/posts/{pending['id']}")).status_code == 404
    # Nobody can engage with it yet.
    comment = await (await authenticate(member)).post(
        f"/v1/posts/{pending['id']}/comments", json={"body": "bump"}
    )
    assert comment.status_code == 409

    owner_client = await authenticate(owner)
    queue = (await owner_client.get(f"/v1/groups/{group['id']}/posts/pending")).json()
    assert [item["id"] for item in queue["items"]] == [pending["id"]]
    reviewed = await owner_client.post(
        f"/v1/groups/{group['id']}/posts/{pending['id']}/review", json={"action": "approve"}
    )
    assert reviewed.json()["approval_status"] == "approved"
    seen = (await (await authenticate(other)).get(f"/v1/groups/{group['id']}/posts")).json()
    assert [item["id"] for item in seen["items"]] == [pending["id"]]


async def test_a_moderators_own_posts_need_no_approval(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner), requires_post_approval=True)

    posted = await (await authenticate(owner)).post(
        f"/v1/groups/{group['id']}/posts", json={"body": "Meeting Sunday", "category": "general"}
    )

    assert posted.json()["approval_status"] is None


async def test_a_muted_member_reads_but_cannot_take_part(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    member = await neighbour(db_session)
    await join(authenticate, member, group["id"])
    member_id = member.id
    owner_client = await authenticate(owner)
    post = (
        await owner_client.post(
            f"/v1/groups/{group['id']}/posts", json={"body": "Welcome", "category": "general"}
        )
    ).json()
    until = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    muted = await owner_client.put(
        f"/v1/groups/{group['id']}/members/{member_id}/mute", json={"until": until}
    )
    assert muted.status_code == 204

    client = await authenticate(member)
    assert (await client.get(f"/v1/groups/{group['id']}/posts")).status_code == 200
    for method, path, body in (
        ("POST", f"/v1/groups/{group['id']}/posts", {"body": "hi", "category": "general"}),
        ("POST", f"/v1/posts/{post['id']}/comments", {"body": "hi"}),
        ("PUT", f"/v1/posts/{post['id']}/reaction", {"kind": "like"}),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code == 403, (path, response.text)
        assert "muted" in response.json()["error"]["message"]

    await (await authenticate(owner)).put(
        f"/v1/groups/{group['id']}/members/{member_id}/mute", json={"until": None}
    )
    again = await (await authenticate(member)).post(
        f"/v1/posts/{post['id']}/comments", json={"body": "hi"}
    )
    assert again.status_code == 201


async def test_pinned_posts_come_first_and_are_limited(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    client = await authenticate(owner)
    ids = [
        (
            await client.post(
                f"/v1/groups/{group['id']}/posts", json={"body": f"post {i}", "category": "general"}
            )
        ).json()["id"]
        for i in range(5)
    ]
    for post_id in ids[:3]:
        pinned = await client.put(
            f"/v1/groups/{group['id']}/posts/{post_id}/pin", json={"pinned": True}
        )
        assert pinned.json()["pinned"] is True

    fourth = await client.put(f"/v1/groups/{group['id']}/posts/{ids[3]}/pin", json={"pinned": True})
    page = (await client.get(f"/v1/groups/{group['id']}/posts")).json()

    assert fourth.status_code == 409
    assert {item["id"] for item in page["pinned"]} == set(ids[:3])
    assert {item["id"] for item in page["items"]} == set(ids[3:])


async def test_a_moderator_removes_a_post_and_it_is_audited(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    member = await neighbour(db_session)
    member_id = str(member.id)
    await join(authenticate, member, group["id"])
    post = (
        await (await authenticate(member)).post(
            f"/v1/groups/{group['id']}/posts", json={"body": "Spam", "category": "general"}
        )
    ).json()

    removed = await (await authenticate(owner)).delete(
        f"/v1/groups/{group['id']}/posts/{post['id']}"
    )

    assert removed.status_code == 204
    assert (await (await authenticate(member)).get(f"/v1/posts/{post['id']}")).status_code == 404
    audit = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.group_post_removed)
        )
    ).scalar_one()
    assert audit.context["author_id"] == member_id


async def test_blocked_authors_are_filtered_from_group_posts(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    noisy, quiet = await neighbour(db_session), await neighbour(db_session)
    await join(authenticate, noisy, group["id"])
    await join(authenticate, quiet, group["id"])
    await (await authenticate(noisy)).post(
        f"/v1/groups/{group['id']}/posts", json={"body": "Noise", "category": "general"}
    )
    await block_user(db_session, blocker=quiet, blocked_id=noisy.id)
    noisy_id = str(noisy.id)

    client = await authenticate(quiet)
    posts = (await client.get(f"/v1/groups/{group['id']}/posts")).json()
    members = (await client.get(f"/v1/groups/{group['id']}/members")).json()

    assert posts["items"] == []
    assert noisy_id not in [item["user"]["id"] for item in members["items"]]


# ---------------------------------------------------------------------------
# The linked chat
# ---------------------------------------------------------------------------
async def test_the_linked_chat_follows_the_group(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    early = await neighbour(db_session)
    await join(authenticate, early, group["id"])

    enabled = (await (await authenticate(owner)).post(f"/v1/groups/{group['id']}/chat")).json()
    chat_id = enabled["chat_conversation_id"]
    assert chat_id

    async def chat_status(person: User) -> str | None:
        row = (
            await db_session.execute(
                select(ConversationMember.status).where(
                    ConversationMember.conversation_id == uuid.UUID(chat_id),
                    ConversationMember.user_id == person.id,
                )
            )
        ).scalar_one_or_none()
        return row.value if isinstance(row, MemberStatus) else row

    assert await chat_status(early) == "active"
    late = await neighbour(db_session)
    await join(authenticate, late, group["id"])
    assert await chat_status(late) == "active"

    await (await authenticate(late)).post(f"/v1/groups/{group['id']}/leave")
    assert await chat_status(late) == "removed"

    early_id = early.id
    await (await authenticate(owner)).put(f"/v1/groups/{group['id']}/bans/{early_id}", json={})
    assert await chat_status(early) == "removed"


async def test_the_linked_chat_cannot_be_managed_from_the_chat(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    group = await create(await authenticate(owner))
    enabled = (await (await authenticate(owner)).post(f"/v1/groups/{group['id']}/chat")).json()
    stranger = await neighbour(db_session)

    client = await authenticate(owner)
    added = await client.post(
        f"/v1/conversations/{enabled['chat_conversation_id']}/members",
        json={"user_ids": [str(stranger.id)]},
    )
    renamed_chat = await client.patch(
        f"/v1/conversations/{enabled['chat_conversation_id']}", json={"title": "Other"}
    )

    assert added.status_code == 400
    assert renamed_chat.status_code == 400
    # Renaming the group renames its chat.
    await client.patch(f"/v1/groups/{group['id']}", json={"name": "Kamakwa Residents"})
    chat = (await client.get(f"/v1/conversations/{enabled['chat_conversation_id']}")).json()
    assert chat["title"] == "Kamakwa Residents"


# ---------------------------------------------------------------------------
# Reporting and data rights
# ---------------------------------------------------------------------------
async def test_reporting_a_group(db_session: AsyncSession, authenticate: Authenticate) -> None:
    group = await create(await authenticate(await neighbour(db_session)))

    response = await (await authenticate(await neighbour(db_session))).post(
        "/v1/reports", json={"target_type": "group", "target_id": group["id"], "reason": "scam"}
    )

    assert response.status_code == 201


async def test_export_lists_memberships_and_created_groups(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await neighbour(db_session)
    client = await authenticate(owner)
    group = await create(client)

    export = (await client.get("/v1/me/export")).json()

    assert [item["group_id"] for item in export["groups"]["memberships"]] == [group["id"]]
    assert [item["id"] for item in export["groups"]["groups_created"]] == [group["id"]]


async def test_purging_an_owner_hands_their_groups_on(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    from app.modules.users.tasks import purge_deleted_accounts

    owner = await neighbour(db_session)
    shared = await create(await authenticate(owner))
    alone = await create(await authenticate(owner), name="Just me", visibility="private")
    member = await neighbour(db_session)
    await join(authenticate, member, shared["id"])
    await db_session.refresh(owner)
    owner.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()

    await purge_deleted_accounts(db_session, settings=settings)

    kept = await db_session.get(Group, uuid.UUID(shared["id"]))
    gone = await db_session.get(Group, uuid.UUID(alone["id"]))
    assert kept is not None and gone is not None
    await db_session.refresh(kept)
    await db_session.refresh(gone)
    assert kept.owner_id == member.id and kept.member_count == 1
    assert gone.deleted_at is not None
    assert (
        await db_session.execute(select(GroupMember).where(GroupMember.user_id == owner.id))
    ).all() == []


async def test_group_settings_are_validated(settings: Any) -> None:
    assert get_settings().group_category_list == settings.group_category_list
    assert set(settings.group_category_list) == {
        "estate",
        "church",
        "chama",
        "students",
        "sports",
        "business",
        "other",
    }
