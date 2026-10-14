"""Direct messages, message requests, and blocking.

The rules here are the ones the brief's "done when" names: message requests
behave as described, and blocked users cannot message each other.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.queue import RecordingJobQueue
from app.modules.messaging.models import Conversation, ConversationState, Message
from app.modules.moderation.models import Report, ReportTargetType
from app.modules.notifications.models import Notification
from app.modules.users.models import Block, User
from app.modules.users.service import block_user
from tests.factories import NYERI_TOWN, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def open_dm(client: httpx.AsyncClient, other: User) -> str:
    response = await client.post("/v1/conversations/direct", json={"user_id": str(other.id)})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def send(client: httpx.AsyncClient, conversation_id: str, body: str) -> httpx.Response:
    return await client.post(f"/v1/conversations/{conversation_id}/messages", json={"body": body})


async def folder(client: httpx.AsyncClient, name: str) -> list[dict[str, Any]]:
    response = await client.get(f"/v1/conversations?folder={name}")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def accepted_pair(
    db_session: AsyncSession, authenticate: Authenticate
) -> tuple[User, User, str]:
    """Two people with an accepted direct conversation."""
    first = await make_user(db_session, at=NYERI_TOWN)
    second = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(first), second)
    assert (await send(await authenticate(first), conversation_id, "Habari")).status_code == 201
    assert (await send(await authenticate(second), conversation_id, "Poa")).status_code == 201
    return first, second, conversation_id


# ---------------------------------------------------------------------------
# Opening a conversation
# ---------------------------------------------------------------------------
async def test_a_pair_has_exactly_one_direct_conversation(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first = await make_user(db_session, at=NYERI_TOWN)
    second = await make_user(db_session, at=NYERI_TOWN)

    one = await open_dm(await authenticate(first), second)
    again = await open_dm(await authenticate(first), second)
    other_way = await open_dm(await authenticate(second), first)

    assert one == again == other_way


async def test_opening_a_conversation_sends_nothing(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    await open_dm(await authenticate(sender), recipient)

    client = await authenticate(recipient)
    assert await folder(client, "requests") == []
    assert await folder(client, "chats") == []


async def test_messaging_yourself_is_refused(user: User, auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.post("/v1/conversations/direct", json={"user_id": str(user.id)})
    assert response.status_code == 400


async def test_messaging_someone_who_does_not_exist_is_a_404(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.post(
        "/v1/conversations/direct", json={"user_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Message requests
# ---------------------------------------------------------------------------
async def test_a_first_message_lands_in_the_recipients_requests(
    db_session: AsyncSession, authenticate: Authenticate, queue: RecordingJobQueue
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)

    response = await send(
        await authenticate(sender), conversation_id, "Is the bike still for sale?"
    )

    assert response.status_code == 201
    assert response.json()["seq"] == 1

    recipient_client = await authenticate(recipient)
    requests = await folder(recipient_client, "requests")
    assert [item["id"] for item in requests] == [conversation_id]
    assert requests[0]["state"] == "request_received"
    assert requests[0]["last_message"]["body"] == "Is the bike still for sale?"
    assert await folder(recipient_client, "chats") == []

    sender_chats = await folder(await authenticate(sender), "chats")
    assert [item["state"] for item in sender_chats] == ["request_sent"]

    # The recipient is notified of the request, and its push is queued.
    notification = (
        await db_session.execute(select(Notification).where(Notification.user_id == recipient.id))
    ).scalar_one()
    assert notification.type == "message_request"
    assert "deliver_notification_push" in queue.names()
    assert "deliver_message_push" not in queue.names()


async def test_the_sender_can_send_only_one_message_until_it_is_accepted(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    client = await authenticate(sender)
    assert (await send(client, conversation_id, "Hello")).status_code == 201

    second = await send(client, conversation_id, "Hello??")

    assert second.status_code == 409
    assert second.json()["error"]["details"]["reason"] == "awaiting_reply"
    count = len(
        (
            await db_session.execute(
                select(Message.id).where(Message.conversation_id == uuid.UUID(conversation_id))
            )
        ).all()
    )
    assert count == 1


async def test_replying_accepts_the_request(
    db_session: AsyncSession, authenticate: Authenticate, queue: RecordingJobQueue
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    await send(await authenticate(sender), conversation_id, "Hello")

    reply = await send(await authenticate(recipient), conversation_id, "Hi, yes it is")

    assert reply.status_code == 201
    assert reply.json()["seq"] == 2
    recipient_chats = await folder(await authenticate(recipient), "chats")
    assert [item["state"] for item in recipient_chats] == ["active"]
    # Now the sender may carry on, and ordinary messages are pushed.
    queue.clear()
    assert (await send(await authenticate(sender), conversation_id, "Great")).status_code == 201
    assert queue.names() == ["deliver_message_push"]


async def test_accepting_explicitly(db_session: AsyncSession, authenticate: Authenticate) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    await send(await authenticate(sender), conversation_id, "Hello")

    response = await (await authenticate(recipient)).post(
        f"/v1/conversations/{conversation_id}/request", json={"action": "accept"}
    )

    assert response.status_code == 200
    assert response.json()["state"] == "active"
    assert (await send(await authenticate(sender), conversation_id, "Thanks")).status_code == 201


async def test_the_sender_cannot_accept_their_own_request(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    client = await authenticate(sender)
    await send(client, conversation_id, "Hello")

    response = await client.post(
        f"/v1/conversations/{conversation_id}/request", json={"action": "accept"}
    )

    assert response.status_code == 404


async def test_a_declined_request_looks_unanswered_to_its_sender(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    await send(await authenticate(sender), conversation_id, "Hello")

    declined = await (await authenticate(recipient)).post(
        f"/v1/conversations/{conversation_id}/request", json={"action": "decline"}
    )
    assert declined.status_code == 200
    assert declined.json()["state"] == "declined"

    sender_client = await authenticate(sender)
    (view,) = await folder(sender_client, "chats")
    assert view["state"] == "request_sent"
    retry = await send(sender_client, conversation_id, "Please reply")
    assert retry.status_code == 409
    assert retry.json()["error"]["details"]["reason"] == "awaiting_reply"
    # And opening the conversation again does not reopen it.
    await open_dm(sender_client, recipient)
    assert (await send(sender_client, conversation_id, "Hello again")).status_code == 409

    # Gone from the recipient's requests.
    assert await folder(await authenticate(recipient), "requests") == []


async def test_the_recipient_can_still_accept_a_request_they_declined(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    await send(await authenticate(sender), conversation_id, "Hello")
    recipient_client = await authenticate(recipient)
    await recipient_client.post(
        f"/v1/conversations/{conversation_id}/request", json={"action": "decline"}
    )

    reply = await send(recipient_client, conversation_id, "Sorry, I missed this")

    assert reply.status_code == 201
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    await db_session.refresh(conversation)
    assert conversation.state == ConversationState.active


async def test_whoever_sends_first_is_the_one_asking(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """Opening an empty conversation does not make you the requester: otherwise
    someone could open one with you and wait for you to "accept" by writing."""
    opener = await make_user(db_session, at=NYERI_TOWN)
    writer = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(opener), writer)

    writer_client = await authenticate(writer)
    assert (await send(writer_client, conversation_id, "Hi")).status_code == 201

    assert [item["state"] for item in await folder(writer_client, "chats")] == ["request_sent"]
    assert (await send(writer_client, conversation_id, "Hi again")).status_code == 409
    opener_requests = await folder(await authenticate(opener), "requests")
    assert [item["id"] for item in opener_requests] == [conversation_id]


async def test_block_and_report_declines_reports_and_blocks(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    sent = (await send(await authenticate(sender), conversation_id, "Send me M-Pesa")).json()

    response = await (await authenticate(recipient)).post(
        f"/v1/conversations/{conversation_id}/block-and-report",
        json={"reason": "scam", "details": "Asked for money"},
    )

    assert response.status_code == 204
    report = (
        await db_session.execute(select(Report).where(Report.reporter_id == recipient.id))
    ).scalar_one()
    assert report.target_type == ReportTargetType.message
    assert str(report.target_id) == sent["id"]
    block = (
        await db_session.execute(select(Block).where(Block.blocker_id == recipient.id))
    ).scalar_one()
    assert block.blocked_id == sender.id
    # Neither of them can see the conversation any more.
    for person in (sender, recipient):
        hidden = await (await authenticate(person)).get(f"/v1/conversations/{conversation_id}")
        assert hidden.status_code == 404


async def test_new_accounts_have_a_lower_daily_request_limit(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN, created_days_ago=0)
    limit = settings.new_account_message_requests_per_day
    recipients = [await make_user(db_session, at=NYERI_TOWN) for _ in range(limit + 1)]

    statuses = []
    for recipient in recipients:
        client = await authenticate(sender)
        conversation_id = await open_dm(client, recipient)
        statuses.append((await send(client, conversation_id, "Hello neighbour")).status_code)

    assert statuses == [201] * limit + [429]


async def test_replies_do_not_count_as_requests(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    """The request limit is about reaching strangers, not answering them."""
    replier = await make_user(db_session, at=NYERI_TOWN, created_days_ago=0)
    for _ in range(settings.new_account_message_requests_per_day + 1):
        stranger = await make_user(db_session, at=NYERI_TOWN)
        conversation_id = await open_dm(await authenticate(stranger), replier)
        await send(await authenticate(stranger), conversation_id, "Hello")
        reply = await send(await authenticate(replier), conversation_id, "Hi")
        assert reply.status_code == 201


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------
async def test_a_blocked_person_cannot_open_a_conversation_either_way(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    blocker = await make_user(db_session, at=NYERI_TOWN)
    blocked = await make_user(db_session, at=NYERI_TOWN)
    await block_user(db_session, blocker=blocker, blocked_id=blocked.id)

    # IDs captured up front: a failed request expires the objects it loaded.
    pairs = ((blocker, blocked.id), (blocked, blocker.id))
    for person, other_id in pairs:
        response = await (await authenticate(person)).post(
            "/v1/conversations/direct", json={"user_id": str(other_id)}
        )
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "User not found"


async def test_blocking_stops_an_existing_conversation_in_both_directions(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    await block_user(db_session, blocker=first, blocked_id=second.id)

    for person in (first, second):
        client = await authenticate(person)
        assert (await send(client, conversation_id, "Are you there?")).status_code == 404
        assert (
            await client.get(f"/v1/conversations/{conversation_id}/messages")
        ).status_code == 404
        assert (await client.get(f"/v1/conversations/{conversation_id}")).status_code == 404
        assert await folder(client, "chats") == []


async def test_a_blocked_sender_cannot_slip_a_request_through(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """Opened before the block, sent after it: still refused."""
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    await block_user(db_session, blocker=recipient, blocked_id=sender.id)

    response = await send(await authenticate(sender), conversation_id, "Hello")

    assert response.status_code == 404
    notifications = (
        await db_session.execute(select(Notification).where(Notification.user_id == recipient.id))
    ).all()
    assert notifications == []


async def test_unblocking_restores_the_conversation(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    second_id = second.id
    client = await authenticate(first)
    blocked = await client.post("/v1/me/blocks", json={"user_id": str(second_id)})
    assert blocked.status_code == 201
    assert (await send(client, conversation_id, "x")).status_code == 404

    assert (await client.delete(f"/v1/me/blocks/{second_id}")).status_code == 204

    assert (await send(client, conversation_id, "Sorry about that")).status_code == 201


# ---------------------------------------------------------------------------
# Content rules
# ---------------------------------------------------------------------------
async def test_a_message_needs_text_or_an_image_but_not_both(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, _second, conversation_id = await accepted_pair(db_session, authenticate)
    client = await authenticate(first)

    empty = await client.post(f"/v1/conversations/{conversation_id}/messages", json={"body": "  "})
    both = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"body": "x", "media_id": str(uuid.uuid4())},
    )

    assert empty.status_code == 400
    assert both.status_code == 400


async def test_an_overlong_message_is_refused(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    first, _second, conversation_id = await accepted_pair(db_session, authenticate)
    response = await send(
        await authenticate(first), conversation_id, "a" * (settings.message_max_length + 1)
    )
    assert response.status_code == 400


async def test_outsiders_cannot_read_or_post(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    _first, _second, conversation_id = await accepted_pair(db_session, authenticate)
    outsider = await authenticate(await make_user(db_session, at=NYERI_TOWN))

    assert (await outsider.get(f"/v1/conversations/{conversation_id}")).status_code == 404
    assert (await outsider.get(f"/v1/conversations/{conversation_id}/messages")).status_code == 404
    assert (await send(outsider, conversation_id, "Hi")).status_code == 404


async def test_sending_is_idempotent_with_a_key(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, _second, conversation_id = await accepted_pair(db_session, authenticate)
    client = await authenticate(first)
    key = str(uuid.uuid4())

    one = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"body": "Niko njiani"},
        headers={"Idempotency-Key": key},
    )
    two = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"body": "Niko njiani"},
        headers={"Idempotency-Key": key},
    )

    assert one.status_code == two.status_code == 201
    assert one.json()["id"] == two.json()["id"]
    assert two.headers.get("Idempotent-Replayed") == "true"


async def test_replying_to_a_message(db_session: AsyncSession, authenticate: Authenticate) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    page = (
        await (await authenticate(first)).get(f"/v1/conversations/{conversation_id}/messages")
    ).json()
    original = page["items"][0]

    reply = await (await authenticate(second)).post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"body": "Replying", "reply_to_id": original["id"]},
    )

    assert reply.status_code == 201
    assert reply.json()["reply_to"] == {"id": original["id"], "seq": original["seq"]}


async def test_replying_to_a_message_from_another_conversation_is_refused(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, _second, conversation_id = await accepted_pair(db_session, authenticate)
    third = await make_user(db_session, at=NYERI_TOWN)
    other_id = await open_dm(await authenticate(first), third)
    await send(await authenticate(first), other_id, "Elsewhere")
    elsewhere = (
        await (await authenticate(first)).get(f"/v1/conversations/{other_id}/messages")
    ).json()

    response = await (await authenticate(first)).post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"body": "x", "reply_to_id": elsewhere["items"][0]["id"]},
    )

    assert response.status_code == 404
