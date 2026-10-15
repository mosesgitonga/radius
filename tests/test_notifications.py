"""Notifications: devices, preferences, collapsing, feed hooks, and push delivery.

Push tasks are called directly, with the test's session in the context, as the
brief asks: assert the job was queued, then run the job and assert what it sent.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.push import RecordingPushProvider
from app.core.queue import RecordingJobQueue
from app.core.realtime import mark_online
from app.modules.auth import service as auth_service
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import DeviceToken, Notification
from app.modules.users.models import User
from app.modules.users.service import block_user
from app.workers.tasks import deliver_message_push, deliver_notification_push, run_retention
from tests.factories import NYERI_TOWN, make_post, make_user
from tests.test_messaging_api import accepted_pair, open_dm, send

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def register(client: httpx.AsyncClient, token: str) -> None:
    response = await client.post(
        "/v1/notifications/devices", json={"token": token, "platform": "android"}
    )
    assert response.status_code == 204, response.text


async def run_queued(queue: RecordingJobQueue, db_session: AsyncSession, name: str) -> list[Any]:
    """Run every queued job of one kind, the way the worker would."""
    tasks: dict[str, Callable[..., Awaitable[Any]]] = {
        "deliver_message_push": deliver_message_push,
        "deliver_notification_push": deliver_notification_push,
    }
    results = []
    for job, args, kwargs in queue.jobs:
        if job == name:
            results.append(await tasks[name]({"session": db_session}, *args, **kwargs))
    return results


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
async def test_registering_a_device_ties_it_to_the_sign_in(
    db_session: AsyncSession, user: User, tokens: Any, auth_client: httpx.AsyncClient
) -> None:
    await register(auth_client, "token-abc")

    device = (await db_session.execute(select(DeviceToken))).scalar_one()
    assert device.user_id == user.id
    assert device.device_session_id == tokens.session_id


async def test_a_token_moves_to_whoever_signs_in_on_that_phone(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first = await make_user(db_session, at=NYERI_TOWN)
    second = await make_user(db_session, at=NYERI_TOWN)
    await register(await authenticate(first), "shared-phone")
    await register(await authenticate(second), "shared-phone")

    devices = (await db_session.execute(select(DeviceToken))).scalars().all()
    assert [device.user_id for device in devices] == [second.id]


async def test_removing_a_device(
    user: User, auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await register(auth_client, "token-abc")

    response = await auth_client.post(
        "/v1/notifications/devices/remove", json={"token": "token-abc"}
    )

    assert response.status_code == 204
    assert (await db_session.execute(select(DeviceToken))).all() == []


async def test_tokens_of_a_signed_out_session_are_not_pushed_to(
    db_session: AsyncSession, settings: Any, authenticate: Authenticate, client: httpx.AsyncClient
) -> None:
    person = await make_user(db_session, at=NYERI_TOWN)
    pair = await auth_service.issue_tokens(db_session, user=person, settings=settings)
    await db_session.flush()
    client.headers["Authorization"] = f"Bearer {pair.access_token}"
    await register(client, "old-phone")
    assert await notifications_service.live_tokens(db_session, person.id) == ["old-phone"]

    await client.post("/v1/auth/logout", json={"refresh_token": pair.refresh_token})

    assert await notifications_service.live_tokens(db_session, person.id) == []


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
async def test_preferences_default_to_on_with_previews_off(
    auth_client: httpx.AsyncClient,
) -> None:
    preferences = (await auth_client.get("/v1/notifications/preferences")).json()

    assert {item["type"] for item in preferences} >= {"message", "comment", "reaction"}
    assert all(item["push_enabled"] and item["in_app_enabled"] for item in preferences)
    assert not any(item["show_preview"] for item in preferences)


async def test_changing_one_preference_keeps_the_others(auth_client: httpx.AsyncClient) -> None:
    first = await auth_client.put(
        "/v1/notifications/preferences/message", json={"show_preview": True}
    )
    second = await auth_client.put(
        "/v1/notifications/preferences/message", json={"push_enabled": False}
    )

    assert first.json()["show_preview"] is True
    assert second.json() == {
        "type": "message",
        "push_enabled": False,
        "in_app_enabled": True,
        "show_preview": True,
    }


# ---------------------------------------------------------------------------
# Feed activity
# ---------------------------------------------------------------------------
async def test_a_comment_notifies_the_post_author(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    neighbour = await make_user(db_session, at=NYERI_TOWN, display_name="Kamau")
    post = await make_post(db_session, redis_client, author=author)

    await (await authenticate(neighbour)).post(
        f"/v1/posts/{post.id}/comments", json={"body": "I saw it"}
    )

    listed = (await (await authenticate(author)).get("/v1/notifications")).json()["items"]
    assert [(item["type"], item["actor"]["display_name"]) for item in listed] == [
        ("comment", "Kamau")
    ]
    assert listed[0]["target_type"] == "post"
    assert listed[0]["target_id"] == str(post.id)
    assert "deliver_notification_push" in queue.names()


async def test_nobody_is_notified_about_themselves(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(author)

    await client.post(f"/v1/posts/{post.id}/comments", json={"body": "Update: found"})
    await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})

    assert (await client.get("/v1/notifications")).json()["items"] == []


async def test_a_reply_notifies_the_comment_author_once(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    commenter = await make_user(db_session, at=NYERI_TOWN)
    replier = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    top = (
        await (await authenticate(commenter)).post(
            f"/v1/posts/{post.id}/comments", json={"body": "Top"}
        )
    ).json()

    await (await authenticate(replier)).post(
        f"/v1/posts/{post.id}/comments", json={"body": "Reply", "parent_id": top["id"]}
    )

    commenter_types = [
        item["type"]
        for item in (await (await authenticate(commenter)).get("/v1/notifications")).json()["items"]
    ]
    author_types = sorted(
        item["type"]
        for item in (await (await authenticate(author)).get("/v1/notifications")).json()["items"]
    )
    assert commenter_types == ["reply"]
    # The author hears about the top comment and the reply, collapsed by post.
    assert author_types == ["comment"]


async def test_a_burst_of_reactions_collapses_into_one_notification(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    reactors = [await make_user(db_session, at=NYERI_TOWN) for _ in range(4)]

    for reactor in reactors:
        await (await authenticate(reactor)).put(
            f"/v1/posts/{post.id}/reaction", json={"kind": "like"}
        )

    items = (await (await authenticate(author)).get("/v1/notifications")).json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "reaction"
    assert items[0]["event_count"] == 4
    assert items[0]["other_actor_count"] == 3
    assert items[0]["actor"]["id"] == str(reactors[-1].id)


async def test_changing_a_reaction_is_not_news(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    reactor = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(reactor)

    await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})
    await client.put(f"/v1/posts/{post.id}/reaction", json={"kind": "love"})

    items = (await (await authenticate(author)).get("/v1/notifications")).json()["items"]
    assert [item["event_count"] for item in items] == [1]


async def test_a_read_notification_is_not_collapsed_into(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    first = await make_user(db_session, at=NYERI_TOWN)
    second = await make_user(db_session, at=NYERI_TOWN)

    await (await authenticate(first)).put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})
    await (await authenticate(author)).post("/v1/notifications/read-all")
    await (await authenticate(second)).put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})

    client = await authenticate(author)
    items = (await client.get("/v1/notifications")).json()["items"]
    assert [(item["read"], item["event_count"]) for item in items] == [(False, 1), (True, 1)]
    assert (await client.get("/v1/notifications/unread-count")).json() == {"count": 1}


async def test_a_blocked_person_causes_no_notification(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    stranger = await make_user(db_session, at=NYERI_TOWN)
    await (await authenticate(stranger)).put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})
    await block_user(db_session, blocker=author, blocked_id=stranger.id)

    # Earlier notifications from them disappear from the list too.
    client = await authenticate(author)
    assert (await client.get("/v1/notifications")).json()["items"] == []
    assert (await client.get("/v1/notifications/unread-count")).json() == {"count": 0}


async def test_marking_someone_elses_notification_is_a_404(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    reactor = await make_user(db_session, at=NYERI_TOWN)
    await (await authenticate(reactor)).put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})
    notification_id = (
        await db_session.execute(select(Notification.id).where(Notification.user_id == author.id))
    ).scalar_one()

    response = await (await authenticate(reactor)).post(f"/v1/notifications/{notification_id}/read")

    assert response.status_code == 404


async def test_switching_off_in_app_keeps_the_push(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    await (await authenticate(author)).put(
        "/v1/notifications/preferences/reaction", json={"in_app_enabled": False}
    )
    reactor = await make_user(db_session, at=NYERI_TOWN)

    await (await authenticate(reactor)).put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})

    assert (await (await authenticate(author)).get("/v1/notifications")).json()["items"] == []
    assert queue.names().count("deliver_notification_push") == 1


# ---------------------------------------------------------------------------
# Push delivery
# ---------------------------------------------------------------------------
async def test_an_offline_recipient_gets_a_push_without_the_text(
    db_session: AsyncSession,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    await register(await authenticate(second), "second-phone")
    queue.clear()

    await send(await authenticate(first), conversation_id, "The password is 1234")
    (result,) = await run_queued(queue, db_session, "deliver_message_push")

    assert result == {"sent": 1, "skipped": 0}
    (message,) = push.messages_to("second-phone")
    assert message.title == first.display_name
    assert message.body == "New message"
    assert "1234" not in message.body
    assert message.data["conversation_id"] == conversation_id
    assert message.data["type"] == "message"


async def test_previews_appear_only_when_opted_in(
    db_session: AsyncSession,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    client = await authenticate(second)
    await register(client, "second-phone")
    await client.put("/v1/notifications/preferences/message", json={"show_preview": True})
    queue.clear()

    await send(await authenticate(first), conversation_id, "Niko njiani")
    await run_queued(queue, db_session, "deliver_message_push")

    assert [message.body for message in push.messages_to("second-phone")] == ["Niko njiani"]


async def test_an_online_recipient_is_not_pushed(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    await register(await authenticate(second), "second-phone")
    await mark_online(redis_client, second.id, "socket-1", ttl_seconds=60)
    queue.clear()

    await send(await authenticate(first), conversation_id, "You there?")
    (result,) = await run_queued(queue, db_session, "deliver_message_push")

    assert result == {"sent": 0, "skipped": 1}
    assert push.sent == []


async def test_a_muted_conversation_is_not_pushed(
    db_session: AsyncSession,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    client = await authenticate(second)
    await register(client, "second-phone")
    await client.put(f"/v1/conversations/{conversation_id}/mute", json={"muted": True})
    queue.clear()

    await send(await authenticate(first), conversation_id, "Hello")
    await run_queued(queue, db_session, "deliver_message_push")

    assert push.sent == []


async def test_pushes_for_one_conversation_are_throttled(
    db_session: AsyncSession,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    await register(await authenticate(second), "second-phone")
    queue.clear()

    for index in range(3):
        await send(await authenticate(first), conversation_id, f"burst {index}")
    await run_queued(queue, db_session, "deliver_message_push")

    assert len(push.messages_to("second-phone")) == 1


async def test_a_block_after_sending_stops_the_push(
    db_session: AsyncSession,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    await register(await authenticate(second), "second-phone")
    queue.clear()
    await send(await authenticate(first), conversation_id, "Hello")

    await block_user(db_session, blocker=second, blocked_id=first.id)
    # Flushed so the task, sharing this session with autoflush off, sees it.
    await db_session.flush()
    await run_queued(queue, db_session, "deliver_message_push")

    assert push.sent == []


async def test_a_dead_token_is_forgotten(
    db_session: AsyncSession,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    await register(await authenticate(second), "uninstalled")
    push.invalid.add("uninstalled")
    queue.clear()

    await send(await authenticate(first), conversation_id, "Hello")
    await run_queued(queue, db_session, "deliver_message_push")

    remaining = await db_session.scalar(select(func.count(DeviceToken.id)))
    assert remaining == 0


async def test_a_message_request_is_pushed_as_a_request(
    db_session: AsyncSession,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN, display_name="Otieno")
    recipient = await make_user(db_session, at=NYERI_TOWN)
    await register(await authenticate(recipient), "recipient-phone")
    conversation_id = await open_dm(await authenticate(sender), recipient)

    await send(await authenticate(sender), conversation_id, "Private text")
    (reason,) = await run_queued(queue, db_session, "deliver_notification_push")

    assert reason == "sent"
    (message,) = push.messages_to("recipient-phone")
    assert message.body == "Otieno wants to send you a message"
    assert "Private text" not in message.body


async def test_a_collapsed_push_names_the_others(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    queue: RecordingJobQueue,
    push: RecordingPushProvider,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    await register(await authenticate(author), "author-phone")
    post = await make_post(db_session, redis_client, author=author)
    for name in ("Wanjiru", "Kamau", "Achieng"):
        reactor = await make_user(db_session, at=NYERI_TOWN, display_name=name)
        await (await authenticate(reactor)).put(
            f"/v1/posts/{post.id}/reaction", json={"kind": "like"}
        )

    # Only the last job runs, as if the throttle had swallowed the first two.
    *_, (_job, args, kwargs) = [
        entry for entry in queue.jobs if entry[0] == "deliver_notification_push"
    ]
    await deliver_notification_push({"session": db_session}, *args, **kwargs)

    (message,) = push.messages_to("author-phone")
    assert message.body == "Achieng and 2 others reacted to your post"


# ---------------------------------------------------------------------------
# Retention and data rights
# ---------------------------------------------------------------------------
async def test_retention_removes_old_notifications_and_stale_tokens(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate, settings: Any
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    await register(await authenticate(author), "old-token")
    post = await make_post(db_session, redis_client, author=author)
    reactor = await make_user(db_session, at=NYERI_TOWN)
    await (await authenticate(reactor)).put(f"/v1/posts/{post.id}/reaction", json={"kind": "like"})

    long_ago = datetime.now(UTC) - timedelta(
        days=max(settings.notification_retention_days, settings.device_token_stale_days) + 1
    )
    await db_session.execute(update(Notification).values(updated_at=long_ago))
    await db_session.execute(update(DeviceToken).values(last_seen_at=long_ago))

    result = await run_retention({"session": db_session})

    assert result["notifications_purged"] == 1
    assert result["stale_device_tokens_deleted"] == 1


async def test_export_includes_messages_sent_but_not_received(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    first, _second, conversation_id = await accepted_pair(db_session, authenticate)
    await register(await authenticate(first), "first-phone-token-12345678")

    export = (await (await authenticate(first)).get("/v1/me/export")).json()

    sent = [item["body"] for item in export["messaging"]["messages_sent"]]
    assert sent == ["Habari"]
    assert "Poa" not in str(export["messaging"])
    assert [item["id"] for item in export["messaging"]["conversations"]] == [conversation_id]
    (device,) = export["notifications"]["devices"]
    assert device["token_ending"] == "12345678"
    assert "first-phone-token" not in str(export)
    assert "message content (no messaging yet)" not in export["not_held"]


async def test_purging_an_account_erases_its_messages_and_devices(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    from app.modules.messaging.models import ConversationMember, MemberStatus, Message
    from app.modules.users.tasks import purge_deleted_accounts

    first, second, conversation_id = await accepted_pair(db_session, authenticate)
    await register(await authenticate(first), "first-phone")
    first.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()

    result = await purge_deleted_accounts(db_session, settings=settings)

    assert result.accounts_purged == 1
    message = (
        await db_session.execute(select(Message).where(Message.sender_id == first.id))
    ).scalar_one()
    assert message.body is None and message.deleted_at is not None
    membership = (
        await db_session.execute(
            select(ConversationMember).where(ConversationMember.user_id == first.id)
        )
    ).scalar_one()
    assert membership.status == MemberStatus.left
    assert (await db_session.execute(select(DeviceToken))).all() == []
    # The other person keeps their side of the conversation.
    shown = (
        await (await authenticate(second)).get(f"/v1/conversations/{conversation_id}/messages")
    ).json()["items"]
    assert [(item["deleted"], item["body"]) for item in shown] == [(True, None), (False, "Poa")]
    assert uuid.UUID(conversation_id)


def test_every_in_app_type_has_push_text() -> None:
    """A type added without wording would fail in the worker, not in a request."""
    from app.modules.notifications.service import _PUSH_TEXT, IN_APP_TYPES, render_activity

    assert set(IN_APP_TYPES) <= set(_PUSH_TEXT)
    for kind in IN_APP_TYPES:
        assert render_activity(kind, actor_name="Kamau", others=2)
