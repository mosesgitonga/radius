"""Reporting and auto-hide.

The rule under test: content reported by N distinct *trusted* reporters is hidden
pending review. Each word in that sentence closes an abuse, and each has a test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.feed.models import Post
from app.modules.moderation.models import (
    REPORT_PRIORITY,
    AuditAction,
    AuditEvent,
    ContentStatus,
    Report,
    ReportReason,
    ReportStatus,
    ReportTargetType,
)
from app.modules.users.models import User, UserStatus
from app.modules.users.service import block_user
from tests.factories import NYERI_TOWN, make_post, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def trusted(db_session: AsyncSession) -> User:
    """An established neighbour: old enough to count towards auto-hide."""
    return await make_user(db_session, at=NYERI_TOWN, created_days_ago=60)


async def report(
    client: httpx.AsyncClient, target_id: object, *, target_type: str = "post", reason: str = "scam"
) -> httpx.Response:
    return await client.post(
        "/v1/reports",
        json={"target_type": target_type, "target_id": str(target_id), "reason": reason},
    )


# ---------------------------------------------------------------------------
# Filing a report
# ---------------------------------------------------------------------------
async def test_a_report_is_filed(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    post = await make_post(db_session, redis_client, author=author)

    response = await report(await authenticate(reporter), post.id, reason="job_scam")

    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    row = (await db_session.execute(select(Report))).scalar_one()
    assert row.priority == REPORT_PRIORITY[ReportReason.job_scam]


def test_job_scams_and_violence_are_prioritised() -> None:
    """The brief requires job scams to be prioritised, and safety comes first."""
    assert REPORT_PRIORITY[ReportReason.violence] > REPORT_PRIORITY[ReportReason.spam]
    assert REPORT_PRIORITY[ReportReason.job_scam] > REPORT_PRIORITY[ReportReason.harassment]
    assert REPORT_PRIORITY[ReportReason.job_scam] > REPORT_PRIORITY[ReportReason.other]


async def test_the_response_does_not_reveal_whether_anything_was_hidden(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Otherwise anyone could measure how many reports it takes."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    reporter = await trusted(db_session)

    body = (await report(await authenticate(reporter), post.id)).json()

    assert set(body) == {"id", "target_type", "target_id", "reason", "status", "created_at"}


@pytest.mark.parametrize("target_type", ["post", "comment", "story", "user"])
async def test_every_phase_2_target_can_be_reported(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    target_type: str,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    author_client = await authenticate(author)
    post = await make_post(db_session, redis_client, author=author)
    targets = {
        "post": post.id,
        "user": author.id,
        "comment": (
            await author_client.post(f"/v1/posts/{post.id}/comments", json={"body": "Hi"})
        ).json()["id"],
        "story": (
            await author_client.post("/v1/stories", json={"kind": "text", "text": "Hi"})
        ).json()["id"],
    }

    response = await report(
        await authenticate(reporter), targets[target_type], target_type=target_type
    )

    assert response.status_code == 201


async def test_you_cannot_report_yourself(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await trusted(db_session)
    post = await make_post(db_session, redis_client, author=author)
    # Captured before any request: a failed request rolls back its savepoint,
    # which expires the ORM objects it touched, and reading an expired attribute
    # afterwards would need a synchronous reload.
    author_id, post_id = author.id, post.id
    client = await authenticate(author)

    assert (await report(client, post_id)).status_code == 400
    assert (await report(client, author_id, target_type="user")).status_code == 400


async def test_you_cannot_report_what_you_cannot_see(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Otherwise reporting would be a way to probe for blocked or hidden content."""
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    post = await make_post(db_session, redis_client, author=author)
    await block_user(db_session, blocker=author, blocked_id=reporter.id)
    await db_session.flush()

    response = await report(await authenticate(reporter), post.id)

    assert response.status_code == 404


async def test_a_nonexistent_target_is_not_found(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    import uuid

    reporter = await trusted(db_session)

    assert (await report(await authenticate(reporter), uuid.uuid4())).status_code == 404


async def test_a_repeat_report_returns_the_original(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(reporter)

    first = await report(client, post.id)
    second = await report(client, post.id, reason="spam")

    assert first.json()["id"] == second.json()["id"]
    assert len((await db_session.execute(select(Report))).all()) == 1


async def test_reporting_is_audited(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    reporter = await trusted(db_session)
    post = await make_post(db_session, redis_client, author=author)

    await report(await authenticate(reporter), post.id)

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.report_submitted)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Auto-hide
# ---------------------------------------------------------------------------
async def test_enough_trusted_reports_hide_a_post(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Send me M-Pesa")

    for _ in range(settings.report_auto_hide_threshold):
        await report(await authenticate(await trusted(db_session)), post.id)

    await db_session.refresh(post)
    assert post.status == ContentStatus.hidden_pending_review
    assert post.hidden_at is not None


async def test_one_fewer_than_the_threshold_leaves_it_visible(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    for _ in range(settings.report_auto_hide_threshold - 1):
        await report(await authenticate(await trusted(db_session)), post.id)

    await db_session.refresh(post)
    assert post.status == ContentStatus.visible


async def test_one_person_reporting_repeatedly_cannot_hide_anything(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    """ "Distinct" is the word that stops one person silencing a neighbour."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    client = await authenticate(await trusted(db_session))

    for reason in ("scam", "spam", "harassment", "other", "violence"):
        await report(client, post.id, reason=reason)

    await db_session.refresh(post)
    assert post.status == ContentStatus.visible


async def test_new_accounts_cannot_hide_content(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    """ "Trusted" is the word that stops a burst of fresh accounts hiding anything.
    Their reports still reach the queue for a human."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    for _ in range(settings.report_auto_hide_threshold + 2):
        newcomer = await make_user(db_session, at=NYERI_TOWN)
        await report(await authenticate(newcomer), post.id)

    await db_session.refresh(post)
    assert post.status == ContentStatus.visible
    reports = (await db_session.execute(select(Report))).scalars().all()
    assert len(reports) == settings.report_auto_hide_threshold + 2
    assert not any(row.counted_towards_auto_hide for row in reports)


async def test_a_suspended_reporter_does_not_count(
    db_session: AsyncSession, settings: Settings
) -> None:
    from app.modules.moderation.service import is_trusted_reporter

    suspended = await make_user(
        db_session, at=NYERI_TOWN, created_days_ago=60, status=UserStatus.suspended
    )

    assert not await is_trusted_reporter(db_session, user=suspended, settings=settings)


async def test_a_reporter_with_an_upheld_report_against_them_does_not_count(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Somebody found to be a bad actor should not be able to silence others."""
    from app.modules.moderation.service import is_trusted_reporter

    bad_actor = await make_user(db_session, at=NYERI_TOWN, created_days_ago=60)
    accuser = await make_user(db_session, at=NYERI_TOWN, created_days_ago=60)
    db_session.add(
        Report(
            reporter_id=accuser.id,
            target_type=ReportTargetType.user,
            target_id=bad_actor.id,
            reason=ReportReason.harassment,
            status=ReportStatus.upheld,
        )
    )
    await db_session.flush()

    assert not await is_trusted_reporter(db_session, user=bad_actor, settings=settings)


async def test_a_verified_neighbour_is_trusted_regardless_of_age(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The badge is Phase 8's signal; when it exists, it wins."""
    from app.modules.moderation.service import is_trusted_reporter

    verified = await make_user(db_session, at=NYERI_TOWN, is_verified_neighbour=True)

    assert await is_trusted_reporter(db_session, user=verified, settings=settings)


async def test_users_are_never_auto_hidden(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    """Auto-suspending a person on a few reports would hand a coordinated group a
    way to silence a neighbour. Reports against people always wait for a human."""
    target = await make_user(db_session, at=NYERI_TOWN)

    for _ in range(settings.report_auto_hide_threshold + 2):
        await report(await authenticate(await trusted(db_session)), target.id, target_type="user")

    await db_session.refresh(target)
    assert target.status == UserStatus.active


async def test_auto_hiding_is_audited_as_a_system_action(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)

    for _ in range(settings.report_auto_hide_threshold):
        await report(await authenticate(await trusted(db_session)), post.id)

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.content_auto_hidden)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_id is None


async def test_a_hidden_post_leaves_the_feed_but_the_author_still_sees_it(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN)
    neighbour = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author, body="Reported")

    for _ in range(settings.report_auto_hide_threshold):
        await report(await authenticate(await trusted(db_session)), post.id)

    neighbour_feed = (await (await authenticate(neighbour)).get("/v1/feed")).json()
    assert "Reported" not in [item["body"] for item in neighbour_feed["items"]]
    author_view = await (await authenticate(author)).get(f"/v1/posts/{post.id}")
    assert author_view.json()["status"] == "hidden_pending_review"


async def test_reports_are_rate_limited(
    db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate
) -> None:
    """Reporting must stay usable, but a report flood is itself abuse."""
    reporter = await trusted(db_session)
    client = await authenticate(reporter)
    statuses = []
    for _ in range(31):
        author = await make_user(db_session, at=NYERI_TOWN)
        post = await make_post(db_session, redis_client, author=author)
        statuses.append((await report(client, post.id)).status_code)

    assert statuses[-1] == 429


async def test_reporting_requires_authentication(client: httpx.AsyncClient) -> None:
    import uuid

    response = await client.post(
        "/v1/reports",
        json={"target_type": "post", "target_id": str(uuid.uuid4()), "reason": "spam"},
    )

    assert response.status_code == 401


async def test_a_post_hidden_by_reports_cannot_be_unhidden_by_more_reports(
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
    settings: Settings,
) -> None:
    """Only visible content moves. Once hidden, further reports do not re-trigger."""
    author = await make_user(db_session, at=NYERI_TOWN)
    post = await make_post(db_session, redis_client, author=author)
    for _ in range(settings.report_auto_hide_threshold + 2):
        await report(await authenticate(await trusted(db_session)), post.id)

    row = (await db_session.execute(select(Post).where(Post.id == post.id))).scalar_one()
    assert row.status == ContentStatus.hidden_pending_review
