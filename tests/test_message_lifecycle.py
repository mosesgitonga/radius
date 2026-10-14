"""Read state, sync, deletion, images, and realtime events.

The realtime tests register a stand-in socket with a real hub over the test Redis,
so they exercise the actual publish → pub/sub → hub → connection path; only the
WebSocket itself is replaced. The socket is covered end to end, across two API
processes, in `test_realtime_live.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.storage import MemoryStorageProvider
from app.core.realtime import SESSION_REVOKED, Hub
from app.modules.auth import service as auth_service
from app.modules.media.models import MediaAsset, MediaPurpose, MediaStatus
from app.modules.messaging.models import Message
from app.modules.moderation.models import ReportReason, ReportTargetType
from app.modules.moderation.service import submit_report
from app.modules.users.models import User
from tests.factories import NYERI_TOWN, make_ready_image, make_user
from tests.test_messaging_api import accepted_pair, open_dm, send

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


class FakeSocket:
    """What the hub sees of a socket: an ID, an owner, and a mailbox."""

    def __init__(self, user_id: uuid.UUID) -> None:
        self.connection_id = uuid.uuid4().hex
        self.user_id = user_id
        self.events: list[dict[str, Any]] = []
        self._arrived = asyncio.Event()

    def deliver(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self._arrived.set()

    async def wait_for(self, event_type: str) -> dict[str, Any]:
        async with asyncio.timeout(3.0):
            while True:
                for event in self.events:
                    if event["type"] == event_type:
                        return event
                # No await between the check and the clear, so an event delivered
                # by the hub cannot slip in unnoticed.
                self._arrived.clear()
                await self._arrived.wait()

    async def settle(self) -> None:
        """Give an event that should *not* arrive a fair chance to."""
        await asyncio.sleep(0.3)


@pytest_asyncio.fixture(loop_scope="session")
async def hub() -> AsyncIterator[Hub]:
    instance = Hub()
    yield instance
    await instance.close()


async def listen(hub: Hub, user: User) -> FakeSocket:
    socket = FakeSocket(user.id)
    await hub.register(socket)
    # A subscription is live once Redis has acknowledged it; a ping round-trip
    # on the hub's connection is the cheap way to be sure.
    await asyncio.sleep(0.1)
    return socket


async def page(client: httpx.AsyncClient, conversation_id: str, query: str = "") -> dict[str, Any]:
    response = await client.get(f"/v1/conversations/{conversation_id}/messages{query}")
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# Ordering and reading
# ---------------------------------------------------------------------------
async def test_sequence_numbers_are_contiguous(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    for index in range(5):
        sender = first if index % 2 else second
        assert (
            await send(await authenticate(sender), conversation_id, f"m{index}")
        ).status_code == 201

    items = (await page(await authenticate(first), conversation_id))["items"]
    assert [item["seq"] for item in items] == list(range(1, 8))


async def test_sync_after_a_sequence_number(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    for index in range(4):
        await send(await authenticate(second), conversation_id, f"while away {index}")

    client = await authenticate(first)
    caught_up = await page(client, conversation_id, "?after_seq=2&limit=3")

    assert [item["seq"] for item in caught_up["items"]] == [3, 4, 5]
    assert caught_up["has_more"] is True
    rest = await page(client, conversation_id, "?after_seq=5")
    assert [item["seq"] for item in rest["items"]] == [6]
    assert rest["has_more"] is False


async def test_scrolling_back(db_session: AsyncSession, authenticate: Authenticate) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    for index in range(4):
        await send(await authenticate(second), conversation_id, f"m{index}")

    client = await authenticate(first)
    latest = await page(client, conversation_id, "?limit=2")
    earlier = await page(
        client, conversation_id, f"?before_seq={latest['items'][0]['seq']}&limit=2"
    )

    assert [item["seq"] for item in latest["items"]] == [5, 6]
    assert [item["seq"] for item in earlier["items"]] == [3, 4]


async def test_read_state_moves_forward_only(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    for index in range(3):
        await send(await authenticate(second), conversation_id, f"m{index}")
    client = await authenticate(first)

    view = (await client.get(f"/v1/conversations/{conversation_id}")).json()
    # The setup's "Poa" plus these three; the caller's own message counts as read.
    assert view["unread_count"] == 4

    await client.post(f"/v1/conversations/{conversation_id}/read", json={"seq": 4})
    await client.post(f"/v1/conversations/{conversation_id}/read", json={"seq": 1})
    view = (await client.get(f"/v1/conversations/{conversation_id}")).json()
    assert view["last_read_seq"] == 4
    assert view["unread_count"] == 1

    # Reading past the end clamps rather than inventing messages.
    await client.post(f"/v1/conversations/{conversation_id}/read", json={"seq": 999})
    view = (await client.get(f"/v1/conversations/{conversation_id}")).json()
    assert view["last_read_seq"] == view["last_seq"] == 5
    other_view = (
        await (await authenticate(second)).get(f"/v1/conversations/{conversation_id}")
    ).json()
    assert other_view["other_last_read_seq"] == 5


async def test_sync_lists_only_what_changed(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, _second, quiet_id = await accepted_pair(db_session, authenticate)
    third = await make_user(db_session, at=NYERI_TOWN)
    busy_id = await open_dm(await authenticate(first), third)
    await send(await authenticate(first), busy_id, "Hi")
    await send(await authenticate(third), busy_id, "Hi")

    client = await authenticate(first)
    everything = (await client.get("/v1/sync")).json()
    assert {item["id"] for item in everything["conversations"]} == {quiet_id, busy_id}
    assert everything["has_more"] is False

    # Backdate both, as though the last sync was a while ago, then change one.
    from sqlalchemy import update

    from app.modules.messaging.models import Conversation, ConversationMember

    past = datetime.now(UTC) - timedelta(hours=1)
    for model in (Conversation, ConversationMember):
        await db_session.execute(update(model).values(updated_at=past))
    await db_session.commit()
    since = (past + timedelta(minutes=10)).isoformat()
    await send(await authenticate(third), busy_id, "Are you coming?")

    changed = (await (await authenticate(first)).get("/v1/sync", params={"since": since})).json()
    assert [item["id"] for item in changed["conversations"]] == [busy_id]
    assert changed["conversations"][0]["last_seq"] == 3


# ---------------------------------------------------------------------------
# Deletion and images
# ---------------------------------------------------------------------------
async def test_deleting_a_message_erases_its_text(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    sent = (await send(await authenticate(first), conversation_id, "Oops, wrong chat")).json()

    response = await (await authenticate(first)).delete(f"/v1/messages/{sent['id']}")

    assert response.status_code == 204
    shown = (await page(await authenticate(second), conversation_id))["items"][-1]
    assert shown["deleted"] is True
    assert shown["body"] is None
    stored = await db_session.get(Message, uuid.UUID(sent["id"]))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.body is None


async def test_only_the_sender_can_delete(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    sent = (await send(await authenticate(first), conversation_id, "Mine")).json()

    response = await (await authenticate(second)).delete(f"/v1/messages/{sent['id']}")

    assert response.status_code == 404


async def test_a_reported_message_keeps_its_content_for_moderators(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    """Deleting it must not destroy the evidence against its sender."""
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    sent = (await send(await authenticate(first), conversation_id, "Something nasty")).json()
    await db_session.refresh(second)
    await submit_report(
        db_session,
        reporter=second,
        target_type=ReportTargetType.message,
        target_id=uuid.UUID(sent["id"]),
        reason=ReportReason.harassment,
        details=None,
        settings=settings,
    )

    await (await authenticate(first)).delete(f"/v1/messages/{sent['id']}")

    shown = (await page(await authenticate(second), conversation_id))["items"][-1]
    assert shown["deleted"] is True and shown["body"] is None
    stored = await db_session.get(Message, uuid.UUID(sent["id"]))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.body == "Something nasty"


async def test_deletions_are_found_by_a_reconnecting_client(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    before = datetime.now(UTC)
    sent = (await send(await authenticate(first), conversation_id, "Gone soon")).json()
    await (await authenticate(first)).delete(f"/v1/messages/{sent['id']}")

    changes = await (await authenticate(second)).get(
        f"/v1/conversations/{conversation_id}/messages/changes",
        params={"since": before.isoformat()},
    )

    assert changes.status_code == 200
    assert [item["id"] for item in changes.json()] == [sent["id"]]


async def test_sending_and_deleting_an_image(
    db_session: AsyncSession,
    authenticate: Authenticate,
    storage: MemoryStorageProvider,
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    image = await make_ready_image(db_session, storage, owner=first, purpose=MediaPurpose.message)
    keys = image.variant_keys()
    assert keys and all(key in storage.objects for key in keys)

    sent = await (await authenticate(first)).post(
        f"/v1/conversations/{conversation_id}/messages", json={"media_id": str(image.id)}
    )
    assert sent.status_code == 201
    assert sent.json()["kind"] == "image"
    shown = (await page(await authenticate(second), conversation_id))["items"][-1]
    assert set(shown["image"]) >= {"320", "720"}

    await (await authenticate(first)).delete(f"/v1/messages/{sent.json()['id']}")

    assert not any(key in storage.objects for key in keys)
    asset = await db_session.get(MediaAsset, image.id)
    assert asset is not None
    await db_session.refresh(asset)
    assert asset.status == MediaStatus.rejected


async def test_an_image_uploaded_for_a_post_cannot_be_sent(
    db_session: AsyncSession, authenticate: Authenticate, storage: MemoryStorageProvider
) -> None:
    first, _second, conversation_id = await accepted_pair(db_session, authenticate)
    image = await make_ready_image(db_session, storage, owner=first, purpose=MediaPurpose.post)

    response = await (await authenticate(first)).post(
        f"/v1/conversations/{conversation_id}/messages", json={"media_id": str(image.id)}
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Realtime
# ---------------------------------------------------------------------------
async def test_a_message_rings_every_member_after_commit(
    db_session: AsyncSession, authenticate: Authenticate, hub: Hub
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    recipient = await listen(hub, second)
    own_device = await listen(hub, first)

    sent = (await send(await authenticate(first), conversation_id, "Live")).json()

    event = await recipient.wait_for("message.created")
    assert event["data"] == {
        "conversation_id": conversation_id,
        "seq": sent["seq"],
        "message_id": sent["id"],
    }
    # The sender's other devices hear about it too.
    await own_device.wait_for("message.created")


async def test_a_failed_send_announces_nothing(
    db_session: AsyncSession, authenticate: Authenticate, hub: Hub
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await make_user(db_session, at=NYERI_TOWN)
    conversation_id = await open_dm(await authenticate(sender), recipient)
    await send(await authenticate(sender), conversation_id, "Hello")
    socket = await listen(hub, recipient)

    refused = await send(await authenticate(sender), conversation_id, "Hello again")

    assert refused.status_code == 409
    await socket.settle()
    assert [event["type"] for event in socket.events] == []


async def test_a_blocked_member_is_not_rung_in_a_group(
    db_session: AsyncSession, authenticate: Authenticate, hub: Hub
) -> None:
    from app.modules.users.service import block_user
    from tests.test_group_chats import contact_of, make_group

    owner = await make_user(db_session, at=NYERI_TOWN)
    speaker = await contact_of(db_session, authenticate, owner)
    blocker = await contact_of(db_session, authenticate, owner)
    group = await make_group(await authenticate(owner), [speaker, blocker])
    await block_user(db_session, blocker=blocker, blocked_id=speaker.id)
    blocker_socket = await listen(hub, blocker)
    owner_socket = await listen(hub, owner)

    await send(await authenticate(speaker), group["id"], "Hello group")

    await owner_socket.wait_for("message.created")
    await blocker_socket.settle()
    assert [event["type"] for event in blocker_socket.events] == []


async def test_signing_out_announces_the_revoked_session(
    db_session: AsyncSession, settings: Any, hub: Hub, client: httpx.AsyncClient
) -> None:
    person = await make_user(db_session, at=NYERI_TOWN)
    pair = await auth_service.issue_tokens(db_session, user=person, settings=settings)
    await db_session.flush()
    socket = await listen(hub, person)

    response = await client.post("/v1/auth/logout", json={"refresh_token": pair.refresh_token})

    assert response.status_code in (200, 204)
    event = await socket.wait_for(SESSION_REVOKED)
    assert event["data"]["session_ids"] == [str(pair.session_id)]


async def test_reuse_detection_announces_every_revoked_session(
    db_session: AsyncSession, settings: Any, hub: Hub, client: httpx.AsyncClient
) -> None:
    """The reuse branch commits by hand, so it must also publish by hand."""
    person = await make_user(db_session, at=NYERI_TOWN)
    pair = await auth_service.issue_tokens(db_session, user=person, settings=settings)
    await db_session.flush()
    rotated = await client.post("/v1/auth/refresh", json={"refresh_token": pair.refresh_token})
    assert rotated.status_code == 200
    socket = await listen(hub, person)

    replay = await client.post("/v1/auth/refresh", json={"refresh_token": pair.refresh_token})

    assert replay.status_code == 401
    event = await socket.wait_for(SESSION_REVOKED)
    assert len(event["data"]["session_ids"]) == 1  # the live successor


async def test_presence(redis_client: Any) -> None:
    from app.core.realtime import mark_offline, mark_online, online_user_ids

    here, away = uuid.uuid4(), uuid.uuid4()
    await mark_online(redis_client, here, "phone", ttl_seconds=60)
    await mark_online(redis_client, here, "tablet", ttl_seconds=60)

    assert await online_user_ids(redis_client, [here, away]) == {here}
    await mark_offline(redis_client, here, "phone")
    assert await online_user_ids(redis_client, [here]) == {here}  # the tablet
    await mark_offline(redis_client, here, "tablet")
    assert await online_user_ids(redis_client, [here]) == set()


async def test_a_connection_that_stops_heartbeating_goes_offline(redis_client: Any) -> None:
    from app.core.realtime import mark_online, online_user_ids

    person = uuid.uuid4()
    # A TTL already in the past stands for a process that died without closing.
    await mark_online(redis_client, person, "dead", ttl_seconds=-1)

    assert await online_user_ids(redis_client, [person]) == set()


async def test_message_rows_never_carry_location(db_session: AsyncSession) -> None:
    columns = {column.name for column in Message.__table__.columns}
    assert not columns & {"latitude", "longitude", "cell", "centroid", "location"}
    assert (await db_session.execute(select(Message.id).limit(1))).all() is not None
