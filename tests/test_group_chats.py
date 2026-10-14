"""Group chats: who joins, who is invited, admin powers, and history visibility."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.modules.users.models import User
from app.modules.users.service import block_user
from tests.factories import NYERI_TOWN, make_user
from tests.test_messaging_api import accepted_pair, folder, open_dm, send

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def contact_of(db_session: AsyncSession, authenticate: Authenticate, owner: User) -> User:
    """Someone with an accepted conversation with `owner`."""
    contact = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(owner), contact)
    await send(await authenticate(owner), conversation_id, "Hi")
    await send(await authenticate(contact), conversation_id, "Hi")
    return contact


async def make_group(
    client: httpx.AsyncClient, members: list[User], title: str = "Estate committee"
) -> dict[str, Any]:
    response = await client.post(
        "/v1/conversations/groups",
        json={"title": title, "member_ids": [str(member.id) for member in members]},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def messages(client: httpx.AsyncClient, conversation_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/v1/conversations/{conversation_id}/messages")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def test_contacts_join_and_strangers_are_invited(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    contact = await contact_of(db_session, authenticate, owner)
    stranger = await make_user(db_session, at=NYERI_TOWN)

    group = await make_group(await authenticate(owner), [contact, stranger])

    assert group["kind"] == "group"
    assert group["my_role"] == "owner"
    assert group["member_count"] == 2  # owner and contact; the stranger is invited

    contact_chats = await folder(await authenticate(contact), "chats")
    assert group["id"] in [item["id"] for item in contact_chats]

    stranger_client = await authenticate(stranger)
    assert [item["id"] for item in await folder(stranger_client, "invitations")] == [group["id"]]
    assert await folder(stranger_client, "chats") == []


async def test_an_invitee_sees_nothing_until_they_accept(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    contact = await contact_of(db_session, authenticate, owner)
    stranger = await make_user(db_session, at=NYERI_TOWN)
    group = await make_group(await authenticate(owner), [contact, stranger])
    await send(await authenticate(owner), group["id"], "Meeting on Saturday")

    stranger_client = await authenticate(stranger)
    assert (
        await stranger_client.get(f"/v1/conversations/{group['id']}/messages")
    ).status_code == 404
    assert (await send(stranger_client, group["id"], "Hello?")).status_code == 404

    accepted = await stranger_client.post(
        f"/v1/conversations/{group['id']}/invitation", json={"action": "accept"}
    )

    assert accepted.status_code == 200
    assert accepted.json()["my_status"] == "active"
    seen = await messages(stranger_client, group["id"])
    # Only what happened from joining on: not the meeting notice before it.
    assert [item["kind"] for item in seen] == ["system"]
    assert seen[0]["system"]["event"] == "member_joined"
    assert (await send(stranger_client, group["id"], "Hello all")).status_code == 201


async def test_declining_an_invitation(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    stranger = await make_user(db_session, at=NYERI_TOWN)
    group = await make_group(await authenticate(owner), [stranger])

    client = await authenticate(stranger)
    response = await client.post(
        f"/v1/conversations/{group['id']}/invitation", json={"action": "decline"}
    )

    assert response.status_code == 200
    assert response.json()["my_status"] == "left"
    assert await folder(client, "invitations") == []


async def test_someone_added_later_does_not_see_earlier_messages(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    first = await contact_of(db_session, authenticate, owner)
    late = await contact_of(db_session, authenticate, owner)
    group = await make_group(await authenticate(owner), [first])
    await send(await authenticate(first), group["id"], "Before you arrived")

    added = await (await authenticate(owner)).post(
        f"/v1/conversations/{group['id']}/members", json={"user_ids": [str(late.id)]}
    )
    assert added.status_code == 200

    seen = await messages(await authenticate(late), group["id"])
    assert all(item["body"] != "Before you arrived" for item in seen)
    assert [item["system"]["event"] for item in seen] == ["members_added"]
    # Everyone else still sees the whole history.
    bodies = [item["body"] for item in await messages(await authenticate(first), group["id"])]
    assert "Before you arrived" in bodies


async def test_members_cannot_use_admin_powers(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    member = await contact_of(db_session, authenticate, owner)
    third = await contact_of(db_session, authenticate, owner)
    group = await make_group(await authenticate(owner), [member, third])
    third_id = third.id

    client = await authenticate(member)
    assert (
        await client.post(
            f"/v1/conversations/{group['id']}/members",
            json={"user_ids": [str(uuid.uuid4())]},
        )
    ).status_code == 403
    assert (
        await client.delete(f"/v1/conversations/{group['id']}/members/{third_id}")
    ).status_code == 403
    assert (
        await client.patch(f"/v1/conversations/{group['id']}", json={"title": "Mine now"})
    ).status_code == 403
    assert (
        await client.put(
            f"/v1/conversations/{group['id']}/members/{third_id}/role", json={"role": "admin"}
        )
    ).status_code == 403


async def test_admins_can_manage_members_but_not_other_admins(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    admin = await contact_of(db_session, authenticate, owner)
    other_admin = await contact_of(db_session, authenticate, owner)
    member = await contact_of(db_session, authenticate, owner)
    group = await make_group(await authenticate(owner), [admin, other_admin, member])
    owner_client = await authenticate(owner)
    for person in (admin, other_admin):
        response = await owner_client.put(
            f"/v1/conversations/{group['id']}/members/{person.id}/role", json={"role": "admin"}
        )
        assert response.status_code == 204
    owner_id, other_admin_id, member_id = owner.id, other_admin.id, member.id

    admin_client = await authenticate(admin)
    renamed = await admin_client.patch(
        f"/v1/conversations/{group['id']}", json={"title": "Kamakwa committee"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Kamakwa committee"
    assert (
        await admin_client.delete(f"/v1/conversations/{group['id']}/members/{other_admin_id}")
    ).status_code == 403
    assert (
        await admin_client.delete(f"/v1/conversations/{group['id']}/members/{owner_id}")
    ).status_code == 403
    assert (
        await admin_client.delete(f"/v1/conversations/{group['id']}/members/{member_id}")
    ).status_code == 204

    removed_client = await authenticate(member)
    assert (await removed_client.get(f"/v1/conversations/{group['id']}")).status_code == 404


async def test_the_owner_leaving_hands_the_chat_on(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    member = await contact_of(db_session, authenticate, owner)
    admin = await contact_of(db_session, authenticate, owner)
    group = await make_group(await authenticate(owner), [member, admin])
    owner_client = await authenticate(owner)
    await owner_client.put(
        f"/v1/conversations/{group['id']}/members/{admin.id}/role", json={"role": "admin"}
    )

    assert (await owner_client.post(f"/v1/conversations/{group['id']}/leave")).status_code == 204

    # The admin outranks the longer-standing plain member.
    view = (await (await authenticate(admin)).get(f"/v1/conversations/{group['id']}")).json()
    assert view["my_role"] == "owner"
    assert (
        await (await authenticate(owner)).get(f"/v1/conversations/{group['id']}")
    ).status_code == 404


async def test_handing_over_ownership(db_session: AsyncSession, authenticate: Authenticate) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    successor = await contact_of(db_session, authenticate, owner)
    group = await make_group(await authenticate(owner), [successor])

    response = await (await authenticate(owner)).put(
        f"/v1/conversations/{group['id']}/members/{successor.id}/role", json={"role": "owner"}
    )

    assert response.status_code == 204
    roles = {
        item["user"]["id"]: item["role"]
        for item in (
            await (await authenticate(successor)).get(f"/v1/conversations/{group['id']}/members")
        ).json()
    }
    assert roles == {str(successor.id): "owner", str(owner.id): "admin"}


async def test_the_member_limit_is_enforced(
    db_session: AsyncSession, authenticate: Authenticate, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "group_chat_member_limit", 3)
    owner = await make_user(db_session, at=NYERI_TOWN)
    people = [await make_user(db_session, at=NYERI_TOWN) for _ in range(3)]

    too_many = await (await authenticate(owner)).post(
        "/v1/conversations/groups",
        json={"title": "Big", "member_ids": [str(person.id) for person in people]},
    )
    assert too_many.status_code == 400

    group = await make_group(await authenticate(owner), people[:2])
    extra = await (await authenticate(owner)).post(
        f"/v1/conversations/{group['id']}/members", json={"user_ids": [str(people[2].id)]}
    )
    assert extra.status_code == 400
    assert extra.json()["error"]["details"]["member_limit"] == 3


async def test_a_blocked_person_cannot_be_added(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN)
    blocked = await make_user(db_session, at=NYERI_TOWN)
    await block_user(db_session, blocker=blocked, blocked_id=owner.id)

    response = await (await authenticate(owner)).post(
        "/v1/conversations/groups",
        json={"title": "Chat", "member_ids": [str(blocked.id)]},
    )

    assert response.status_code == 404


async def test_blocked_people_are_filtered_out_of_a_shared_group(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """Both stay in the group; each simply stops seeing the other."""
    owner = await make_user(db_session, at=NYERI_TOWN)
    first = await contact_of(db_session, authenticate, owner)
    second = await contact_of(db_session, authenticate, owner)
    group = await make_group(await authenticate(owner), [first, second])
    await send(await authenticate(first), group["id"], "From first, before the block")
    await block_user(db_session, blocker=second, blocked_id=first.id)
    await send(await authenticate(first), group["id"], "From first, after the block")
    first_id = str(first.id)

    second_client = await authenticate(second)
    seen = await messages(second_client, group["id"])
    assert all(item["sender"] is None or item["sender"]["id"] != first_id for item in seen)
    members = (await second_client.get(f"/v1/conversations/{group['id']}/members")).json()
    assert first_id not in [item["user"]["id"] for item in members]

    # The owner, who blocked nobody, still sees everything.
    owner_seen = await messages(await authenticate(owner), group["id"])
    assert [item["body"] for item in owner_seen if item["kind"] == "text"] == [
        "From first, before the block",
        "From first, after the block",
    ]


async def test_system_messages_name_people_by_id(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    owner = await make_user(db_session, at=NYERI_TOWN, display_name="Wanjiku")
    contact = await contact_of(db_session, authenticate, owner)
    group = await make_group(await authenticate(owner), [contact])

    seen = await messages(await authenticate(owner), group["id"])

    created = seen[0]
    assert created["kind"] == "system"
    assert created["sender"] is None
    assert created["body"] is None
    assert created["system"]["event"] == "created"
    assert created["system"]["actor_id"] == str(owner.id)
    assert "Wanjiku" not in str(created)


async def test_groups_need_a_name_and_a_member(
    user: User, auth_client: httpx.AsyncClient, settings: Settings
) -> None:
    user_id = str(user.id)
    blank = await auth_client.post(
        "/v1/conversations/groups", json={"title": "   ", "member_ids": [str(uuid.uuid4())]}
    )
    only_me = await auth_client.post(
        "/v1/conversations/groups", json={"title": "Me", "member_ids": [user_id]}
    )

    assert blank.status_code == 400
    assert only_me.status_code == 400


async def test_direct_conversations_have_no_group_actions(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, _second, conversation_id = await accepted_pair(db_session, authenticate)
    response = await (await authenticate(first)).post(f"/v1/conversations/{conversation_id}/leave")
    assert response.status_code == 400
