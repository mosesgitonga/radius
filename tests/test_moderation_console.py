"""The moderation console: the queue, content and account actions, bans,
restrictions, the audit log, and what the person affected is told."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.sms import ConsoleSmsProvider
from app.modules.auth.models import DeviceSession
from app.modules.feed.models import Post
from app.modules.moderation import banlist, console
from app.modules.moderation.models import (
    AuditAction,
    AuditEvent,
    BannedDevice,
    BannedPhone,
    ContentStatus,
    Report,
    ReportStatus,
)
from app.modules.notifications.models import Notification, NotificationType
from app.modules.staff.models import StaffRole
from app.modules.users.models import User, UserStatus
from tests.factories import NYERI_TOWN, make_post, make_staff, make_user, phone_of, staff_token

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def moderator(db_session: AsyncSession) -> dict[str, str]:
    issued = await make_staff(db_session)
    return _bearer(await staff_token(db_session, issued.account))


@pytest.fixture
async def admin(db_session: AsyncSession) -> dict[str, str]:
    issued = await make_staff(db_session, role=StaffRole.admin)
    return _bearer(await staff_token(db_session, issued.account))


async def trusted(db_session: AsyncSession, **kwargs: Any) -> User:
    return await make_user(db_session, at=NYERI_TOWN, created_days_ago=60, **kwargs)


async def report(
    client: httpx.AsyncClient, target_id: object, *, target_type: str = "post", reason: str = "spam"
) -> httpx.Response:
    response = await client.post(
        "/v1/reports",
        json={"target_type": target_type, "target_id": str(target_id), "reason": reason},
    )
    assert response.status_code == 201, response.text
    return response


async def post_status(db_session: AsyncSession, post_id: uuid.UUID) -> ContentStatus | None:
    return await db_session.scalar(
        select(Post.status).where(Post.id == post_id).execution_options(populate_existing=True)
    )


async def notices(db_session: AsyncSession, user_id: uuid.UUID) -> list[Notification]:
    return list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == user_id,
                    Notification.type == NotificationType.account_notice,
                )
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------
async def test_the_queue_puts_the_worst_first(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    spam = await make_post(db_session, redis_client, author=author, body="Buy cheap phones")
    violent = await make_post(db_session, redis_client, author=author, body="A threat")
    spam_id, violent_id = spam.id, violent.id
    await report(await authenticate(reporter), spam_id, reason="spam")
    await report(await authenticate(reporter), violent_id, reason="violence")

    response = await client.get("/v1/admin/queue", headers=moderator)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["target_id"] for item in items] == [str(violent_id), str(spam_id)]
    first = items[0]
    assert first["reasons"] == ["violence"]
    assert first["report_count"] == 1
    assert first["excerpt"] == "A threat"
    assert first["author"]["id"] == str(author.id)
    # No phone number, no location, for anyone.
    assert "phone_number" not in first["author"]
    assert not any("cell" in key or "centroid" in key for key in first["author"])


async def test_one_row_per_thing_however_many_reports(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    post_id = post.id
    for reason in ("spam", "scam"):
        await report(await authenticate(await trusted(db_session)), post_id, reason=reason)

    items = (await client.get("/v1/admin/queue", headers=moderator)).json()["items"]
    assert len(items) == 1
    assert items[0]["report_count"] == 2
    assert items[0]["trusted_report_count"] == 2
    assert items[0]["reasons"] == ["scam", "spam"]


async def test_held_job_posts_are_in_the_queue_without_a_report(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    employer = await trusted(db_session)
    held = await (await authenticate(employer)).post(
        "/v1/services",
        json={
            "kind": "hiring",
            "category": "house_help",
            "title": "House help needed",
            "description": "Pay a registration fee of KES 1,500 to secure the job.",
            "job_type": "full_time",
        },
    )
    assert held.status_code == 201, held.text
    items = (await client.get("/v1/admin/queue", headers=moderator)).json()["items"]
    assert [item["target_id"] for item in items] == [held.json()["id"]]
    assert items[0]["held"] is True
    assert items[0]["report_count"] == 0
    assert items[0]["priority"] == console.HELD_JOB_SCAM_PRIORITY


async def test_the_queue_pages_without_repeats(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    ids = []
    for index in range(5):
        post = await make_post(db_session, redis_client, author=author, body=f"Post {index}")
        ids.append(str(post.id))
    for post_id in ids:
        await report(await authenticate(reporter), post_id)

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/v1/admin/queue", headers=moderator, params=params)).json()
        seen += [item["target_id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))

    bad = await client.get("/v1/admin/queue", headers=moderator, params={"cursor": "nonsense"})
    assert bad.status_code == 400


async def test_content_detail_shows_the_text_and_the_reports(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    post = await make_post(db_session, redis_client, author=author, body="The whole text")
    post_id = post.id
    await (await authenticate(reporter)).post(
        "/v1/reports",
        json={
            "target_type": "post",
            "target_id": str(post_id),
            "reason": "harassment",
            "details": "Aimed at my neighbour",
        },
    )
    response = await client.get(f"/v1/admin/content/post/{post_id}", headers=moderator)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"]["text"] == "The whole text"
    assert body["content"]["status"] == "visible"
    assert body["reports"][0]["details"] == "Aimed at my neighbour"
    assert body["reports"][0]["trusted"] is True

    missing = await client.get(f"/v1/admin/content/post/{uuid.uuid4()}", headers=moderator)
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Content actions
# ---------------------------------------------------------------------------
async def test_removing_content_upholds_reports_and_tells_the_author(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    neighbour = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    post_id, author_id = post.id, author.id
    await report(await authenticate(reporter), post_id, reason="scam")

    response = await client.post(
        f"/v1/admin/content/post/{post_id}/remove",
        headers=moderator,
        json={"reason": "Advance-fee scam"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"reports_resolved": 1}

    stored = await db_session.get(Post, post_id)
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.status == ContentStatus.removed
    row = (await db_session.execute(select(Report))).scalar_one()
    assert row.status == ReportStatus.upheld
    assert row.resolution_note == "Advance-fee scam"
    assert (await (await authenticate(neighbour)).get(f"/v1/posts/{post_id}")).status_code == 404

    audit = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.content_removed)
        )
    ).scalar_one()
    assert audit.actor_type == "moderator"
    assert audit.context["author_id"] == str(author_id)
    assert [n.data for n in await notices(db_session, author_id)] == [{"notice": "content_removed"}]

    again = await client.post(
        f"/v1/admin/content/post/{post_id}/remove", headers=moderator, json={"reason": "Again"}
    )
    assert again.status_code == 409


async def test_restoring_auto_hidden_content_dismisses_the_reports(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    post_id = post.id
    for _ in range(settings.report_auto_hide_threshold):
        await report(await authenticate(await trusted(db_session)), post_id)
    assert await post_status(db_session, post_id) == ContentStatus.hidden_pending_review

    response = await client.post(
        f"/v1/admin/content/post/{post_id}/restore", headers=moderator, json={"note": "Fine"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["reports_resolved"] == settings.report_auto_hide_threshold
    assert await post_status(db_session, post_id) == ContentStatus.visible
    statuses = set((await db_session.execute(select(Report.status))).scalars())
    assert statuses == {ReportStatus.dismissed}
    assert (await client.get("/v1/admin/queue", headers=moderator)).json()["items"] == []


async def test_resolving_without_acting(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    post_id = post.id
    await report(await authenticate(await trusted(db_session)), post_id)
    url = f"/v1/admin/content/post/{post_id}/resolve"
    response = await client.post(url, headers=moderator, json={"outcome": "dismissed"})
    assert response.json() == {"reports_resolved": 1}
    again = await client.post(url, headers=moderator, json={"outcome": "dismissed"})
    assert again.status_code == 409


async def test_removing_a_reported_message(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    sender = await make_user(db_session, at=NYERI_TOWN)
    recipient = await trusted(db_session)
    opened = await (await authenticate(sender)).post(
        "/v1/conversations/direct", json={"user_id": str(recipient.id)}
    )
    assert opened.status_code in (200, 201), opened.text
    conversation_id = opened.json()["id"]
    sent = await (await authenticate(sender)).post(
        f"/v1/conversations/{conversation_id}/messages", json={"body": "Send me KES 500"}
    )
    message_id = sent.json()["id"]
    await report(await authenticate(recipient), message_id, target_type="message", reason="scam")

    detail = await client.get(f"/v1/admin/content/message/{message_id}", headers=moderator)
    assert detail.json()["content"]["text"] == "Send me KES 500"
    removed = await client.post(
        f"/v1/admin/content/message/{message_id}/remove", headers=moderator, json={"reason": "Scam"}
    )
    assert removed.status_code == 200, removed.text
    after = await client.get(f"/v1/admin/content/message/{message_id}", headers=moderator)
    assert after.json()["content"]["status"] == "deleted"
    # Kept for the moderator as evidence.
    assert after.json()["content"]["text"] == "Send me KES 500"

    restore = await client.post(
        f"/v1/admin/content/message/{message_id}/restore", headers=moderator, json={}
    )
    assert restore.status_code == 400


async def test_a_person_is_not_content(
    client: httpx.AsyncClient, db_session: AsyncSession, moderator: dict[str, str]
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    response = await client.post(
        f"/v1/admin/content/user/{user.id}/remove",
        headers=moderator,
        json={"reason": "Not allowed"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Account actions
# ---------------------------------------------------------------------------
async def test_a_warning_reaches_the_person(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    user_id = user.id
    response = await client.post(
        f"/v1/admin/users/{user_id}/warn",
        headers=moderator,
        json={"reason": "Please keep posts on topic"},
    )
    assert response.status_code == 201, response.text
    standing = (await (await authenticate(user)).get("/v1/me/standing")).json()
    assert [w["reason"] for w in standing["warnings"]] == ["Please keep posts on topic"]
    assert len(await notices(db_session, user_id)) == 1


async def test_suspension_is_read_only_and_lapses(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    user_id = user.id
    response = await client.post(
        f"/v1/admin/users/{user_id}/suspend",
        headers=moderator,
        json={"reason": "Repeated spam", "days": 3},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "suspended"

    me = await authenticate(user)
    assert (await me.get("/v1/me/standing")).json()["status"] == "suspended"
    blocked = await me.post("/v1/posts", json={"body": "Hello", "category": "general"})
    assert blocked.status_code == 403

    # Past its end, it counts as lifted before the sweep, and the sweep says so.
    await db_session.refresh(user)
    user.suspended_until = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()
    allowed = await (await authenticate(user)).post(
        "/v1/posts", json={"body": "Hello again", "category": "general"}
    )
    assert allowed.status_code == 201, allowed.text
    assert await console.lift_expired_suspensions(db_session) == 1
    await db_session.refresh(user)
    assert user.status == UserStatus.active


async def test_restricted_accounts_are_slowed_not_silenced(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    user = await trusted(db_session)
    other = await trusted(db_session)
    user_id, other_id = user.id, other.id
    response = await client.post(
        f"/v1/admin/users/{user_id}/restrict",
        headers=moderator,
        json={"reason": "Under review", "days": 7},
    )
    assert response.status_code == 200, response.text
    assert response.json()["restricted"] is True

    me = await authenticate(user)
    # Posts are held: the author sees theirs, nobody else does, the queue has it.
    created = await me.post("/v1/posts", json={"body": "My new post", "category": "general"})
    assert created.status_code == 201, created.text
    post_id = created.json()["post"]["id"]
    assert created.json()["post"]["status"] == "hidden_pending_review"
    assert (await (await authenticate(other)).get(f"/v1/posts/{post_id}")).status_code == 404
    queue = (await client.get("/v1/admin/queue", headers=moderator)).json()["items"]
    assert [item["target_id"] for item in queue] == [post_id]
    assert queue[0]["held"] is True

    # No first contact: opening a conversation sends nothing, the first message is
    # the request, and that is refused.
    opened = await (await authenticate(user)).post(
        "/v1/conversations/direct", json={"user_id": str(other_id)}
    )
    assert opened.status_code in (200, 201), opened.text
    send = await (await authenticate(user)).post(
        f"/v1/conversations/{opened.json()['id']}/messages", json={"body": "Hi"}
    )
    assert send.status_code == 403
    assert send.json()["error"]["details"]["reason"] == "restricted"

    # No job posts.
    job = await (await authenticate(user)).post(
        "/v1/services",
        json={
            "kind": "hiring",
            "category": "delivery",
            "title": "Rider wanted",
            "job_type": "casual",
        },
    )
    assert job.status_code == 403

    # Lifting it.
    lifted = await client.post(f"/v1/admin/users/{user_id}/unrestrict", headers=moderator, json={})
    assert lifted.json()["restricted"] is False


async def test_enough_trusted_reports_restrict_a_person(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    target = await make_user(db_session, at=NYERI_TOWN)
    target_id = target.id
    for _ in range(settings.report_auto_restrict_threshold):
        await report(
            await authenticate(await trusted(db_session)),
            target_id,
            target_type="user",
            reason="harassment",
        )
    await db_session.refresh(target)
    assert target.restricted_until is not None
    assert target.restricted_until > datetime.now(UTC) + timedelta(days=6)
    assert target.restriction_reason == "automatic: reports"
    # Restricted, never suspended or hidden.
    assert target.status == UserStatus.active


async def test_untrusted_reports_do_not_restrict(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    target = await make_user(db_session, at=NYERI_TOWN)
    for _ in range(settings.report_auto_restrict_threshold):
        newcomer = await make_user(db_session, at=NYERI_TOWN)
        await report(await authenticate(newcomer), target.id, target_type="user")
    await db_session.refresh(target)
    assert target.restricted_until is None


async def test_badge_revoke_and_lock(
    client: httpx.AsyncClient, db_session: AsyncSession, moderator: dict[str, str]
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN, is_verified_neighbour=True)
    response = await client.post(
        f"/v1/admin/users/{user.id}/badge/revoke", headers=moderator, json={"lock": True}
    )
    assert response.json()["is_verified_neighbour"] is False
    assert response.json()["badge_locked"] is True
    unlocked = await client.post(f"/v1/admin/users/{user.id}/badge/unlock", headers=moderator)
    assert unlocked.json()["badge_locked"] is False


async def test_user_history(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    moderator: dict[str, str],
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    user_id = user.id
    post = await make_post(db_session, redis_client, author=user)
    post_id = post.id
    await report(await authenticate(reporter), user_id, target_type="user", reason="harassment")
    await report(await authenticate(reporter), post_id)
    await client.post(
        f"/v1/admin/content/post/{post_id}/remove", headers=moderator, json={"reason": "Spam"}
    )
    await client.post(f"/v1/admin/users/{user_id}/warn", headers=moderator, json={"reason": "Stop"})

    history = (await client.get(f"/v1/admin/users/{user_id}", headers=moderator)).json()
    assert len(history["reports_against"]) == 1
    assert history["warnings"][0]["reason"] == "Stop"
    assert history["content_removed_count"] == 1
    actions = {event["action"] for event in history["actions"]}
    assert {"content_removed", "user_warned"} <= actions


# ---------------------------------------------------------------------------
# Bans
# ---------------------------------------------------------------------------
async def test_only_admins_ban(
    client: httpx.AsyncClient, db_session: AsyncSession, moderator: dict[str, str]
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    response = await client.post(
        f"/v1/admin/users/{user.id}/ban", headers=moderator, json={"reason": "Scammer"}
    )
    assert response.status_code == 403
    assert (await client.get("/v1/admin/audit", headers=moderator)).status_code == 403
    assert (await client.get("/v1/admin/bans/phones", headers=moderator)).status_code == 403


async def test_a_ban_reaches_the_number_and_the_devices(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    authenticate: Authenticate,
    admin: dict[str, str],
    settings: Settings,
    sms: ConsoleSmsProvider,
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    user_id, phone = user.id, phone_of(user)
    me = await authenticate(user)
    token = me.headers["Authorization"]
    # The account signed in once from an installation the ban should reach.
    device_session = (
        await db_session.execute(select(DeviceSession).where(DeviceSession.user_id == user_id))
    ).scalar_one()
    device_session.device_id_hash = banlist.device_hash("install-1234", settings)
    await db_session.flush()

    response = await client.post(
        f"/v1/admin/users/{user_id}/ban",
        headers=admin,
        json={"reason": "Scammer", "remove_content": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "banned"

    # Signed out at once.
    assert (await client.get("/v1/me", headers={"Authorization": token})).status_code in (401, 403)
    assert (await db_session.execute(select(BannedPhone))).scalar_one().user_id == user_id
    assert (await db_session.execute(select(BannedDevice))).scalar_one().device_hash == (
        banlist.device_hash("install-1234", settings)
    )

    # The banned number is answered as usual and sent nothing.
    client.headers.pop("Authorization", None)
    sms.clear()
    asked = await client.post("/v1/auth/otp/request", json={"phone_number": phone})
    assert asked.status_code == 200
    assert not sms.sent

    # A fresh number on the banned device: the code checks out, the sign-in fails
    # like an expired code.
    fresh = "+254711000999"
    assert (
        await client.post("/v1/auth/otp/request", json={"phone_number": fresh})
    ).status_code == 200
    code = next(
        part.strip(".,")
        for part in (sms.last_message() or "").split()
        if part.strip(".,").isdigit() and len(part.strip(".,")) == 6
    )
    verify = await client.post(
        "/v1/auth/otp/verify",
        json={"phone_number": fresh, "code": code, "device_id": "install-1234"},
    )
    assert verify.status_code == 401
    assert verify.json()["error"]["message"] == "That code is not valid or has expired"

    # Lifting the ban lifts both.
    lifted = await client.post(f"/v1/admin/users/{user_id}/unban", headers=admin)
    assert lifted.json()["status"] == "active"
    assert (await db_session.execute(select(BannedPhone))).first() is None
    assert (await db_session.execute(select(BannedDevice))).first() is None


async def test_the_device_id_is_stored_only_as_a_hash(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: ConsoleSmsProvider,
    settings: Settings,
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    phone = phone_of(user)
    client.headers.pop("Authorization", None)
    await client.post("/v1/auth/otp/request", json={"phone_number": phone})
    code = next(
        part.strip(".,")
        for part in (sms.last_message() or "").split()
        if part.strip(".,").isdigit() and len(part.strip(".,")) == 6
    )
    verify = await client.post(
        "/v1/auth/otp/verify",
        json={"phone_number": phone, "code": code, "device_id": "my-installation-id"},
    )
    assert verify.status_code == 200, verify.text
    stored = (
        await db_session.execute(
            select(DeviceSession.device_id_hash).where(DeviceSession.user_id == user.id)
        )
    ).scalar_one()
    assert stored == banlist.device_hash("my-installation-id", settings)
    assert "my-installation-id" not in stored


async def test_phone_ban_list(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, str]
) -> None:
    created = await client.post(
        "/v1/admin/bans/phones",
        headers=admin,
        json={"phone_number": "0712 000 111", "reason": "Known scammer"},
    )
    assert created.status_code == 201, created.text
    ban_id = created.json()["id"]
    assert "phone" not in created.json()
    duplicate = await client.post(
        "/v1/admin/bans/phones", headers=admin, json={"phone_number": "+254712000111"}
    )
    assert duplicate.status_code == 409
    listed = (await client.get("/v1/admin/bans/phones", headers=admin)).json()["items"]
    assert [entry["id"] for entry in listed] == [ban_id]
    deleted = await client.delete(f"/v1/admin/bans/phones/{ban_id}", headers=admin)
    assert deleted.status_code == 204
    assert (await client.get("/v1/admin/bans/phones", headers=admin)).json()["items"] == []


async def test_the_audit_log(
    client: httpx.AsyncClient, db_session: AsyncSession, admin: dict[str, str]
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    await client.post(f"/v1/admin/users/{user.id}/warn", headers=admin, json={"reason": "Careful"})
    response = await client.get(
        "/v1/admin/audit",
        headers=admin,
        params={"actor_type": "moderator", "action": "user_warned"},
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["target_id"] == str(user.id)
