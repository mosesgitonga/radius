"""Circle: mutual connections that reach beyond the neighbourhood.

Decisions C.1-C.6. Requests are mutual and declines silent; refusals all look
alike; the contact rule (M.7) applies; a block ends everything; people beyond
the viewer's feed radius are shown by their town only, with no distance.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.queue import RecordingJobQueue
from app.modules.circle import service as circle_service
from app.modules.circle.models import CircleLink, CircleRequest, CircleRequestStatus
from app.modules.feed import service as feed_service
from app.modules.feed.models import Audience
from app.modules.notifications.models import Notification
from app.modules.users.models import CircleRequestPolicy, User
from app.modules.users.service import block_user
from app.modules.users.tasks import purge_deleted_accounts
from tests.factories import (
    KARATINA,
    NAIROBI,
    NYERI_TOWN,
    make_area,
    make_post,
    make_user,
    offset_km,
)

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def person(db_session: AsyncSession, **kwargs: Any) -> User:
    return await make_user(
        db_session,
        at=kwargs.pop("at", NYERI_TOWN),
        created_days_ago=kwargs.pop("age", 30),
        **kwargs,
    )


def uid(user: object) -> str:
    """A user's ID without touching the database: after a refused request the
    object may be expired, and reloading it here would need a greenlet."""
    state = inspect(user)
    assert state is not None
    identity = state.identity
    assert identity is not None
    return str(identity[0])


async def ask(authenticate: Authenticate, sender: User, recipient: User) -> httpx.Response:
    return await (await authenticate(sender)).post(
        "/v1/circle/requests", json={"user_id": uid(recipient)}
    )


async def linked(
    db_session: AsyncSession, authenticate: Authenticate, first: User, second: User
) -> None:
    sent = await ask(authenticate, first, second)
    assert sent.status_code == 201, sent.text
    request_id = sent.json()["request"]["id"]
    accepted = await (await authenticate(second)).post(f"/v1/circle/requests/{request_id}/accept")
    assert accepted.status_code == 204, accepted.text


async def requests(authenticate: Authenticate, user: User, direction: str) -> list[dict[str, Any]]:
    response = await (await authenticate(user)).get(f"/v1/circle/requests?direction={direction}")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def circle(authenticate: Authenticate, user: User) -> list[dict[str, Any]]:
    response = await (await authenticate(user)).get("/v1/circle")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def relationship(authenticate: Authenticate, viewer: User, other: User) -> str:
    response = await (await authenticate(viewer)).get(f"/v1/users/{uid(other)}")
    assert response.status_code == 200, response.text
    return str(response.json()["relationship"])


async def circle_notices(db_session: AsyncSession, user: User) -> list[Notification]:
    return list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == user.id, Notification.type == "circle"
                )
            )
        ).scalars()
    )


async def age_request(
    db_session: AsyncSession, request_id: str, *, created: int, responded: int | None = None
) -> None:
    """Move a request `created` days into the past, and its answer `responded`."""
    request = await db_session.get(CircleRequest, request_id)
    assert request is not None
    now = datetime.now(UTC)
    request.created_at = now - timedelta(days=created)
    if responded is not None:
        request.responded_at = now - timedelta(days=responded)
    await db_session.flush()


def refusal(response: httpx.Response) -> str | None:
    reason = response.json().get("error", {}).get("details", {}).get("reason")
    return str(reason) if reason is not None else None


# ---------------------------------------------------------------------------
# Asking and accepting
# ---------------------------------------------------------------------------
async def test_asking_and_accepting_joins_both_circles(
    db_session: AsyncSession, authenticate: Authenticate, queue: RecordingJobQueue
) -> None:
    amina = await person(db_session, display_name="Amina")
    baraka = await person(db_session, at=NAIROBI, display_name="Baraka")

    sent = await ask(authenticate, amina, baraka)
    assert sent.status_code == 201, sent.text
    assert sent.json()["relationship"] == "request_sent"
    assert await relationship(authenticate, amina, baraka) == "request_sent"
    assert await relationship(authenticate, baraka, amina) == "request_received"

    incoming = await requests(authenticate, baraka, "incoming")
    assert [item["user"]["id"] for item in incoming] == [uid(amina)]
    assert incoming[0]["direction"] == "incoming"
    (notice,) = await circle_notices(db_session, baraka)
    assert notice.data["event"] == "request"
    pushes = [args[4] for name, args, _ in queue.jobs if name == "deliver_notification_push"]
    assert pushes[-1]["push_text"] == "Amina wants to add you to their circle"

    accepted = await (await authenticate(baraka)).post(
        f"/v1/circle/requests/{incoming[0]['id']}/accept"
    )
    assert accepted.status_code == 204
    (accepted_notice,) = await circle_notices(db_session, amina)
    assert accepted_notice.data["event"] == "accepted"

    assert [item["user"]["id"] for item in await circle(authenticate, amina)] == [uid(baraka)]
    assert [item["user"]["id"] for item in await circle(authenticate, baraka)] == [uid(amina)]
    assert await relationship(authenticate, amina, baraka) == "in_circle"
    assert await requests(authenticate, baraka, "incoming") == []
    assert await requests(authenticate, amina, "outgoing") == []


async def test_crossed_requests_accept_each_other(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    await ask(authenticate, amina, baraka)

    crossed = await ask(authenticate, baraka, amina)
    assert crossed.status_code == 201, crossed.text
    assert crossed.json()["relationship"] == "in_circle"
    assert crossed.json()["request"] is None
    assert await relationship(authenticate, amina, baraka) == "in_circle"


async def test_asking_twice_or_asking_a_member_is_harmless(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    first = (await ask(authenticate, amina, baraka)).json()
    again = (await ask(authenticate, amina, baraka)).json()
    assert again["request"]["id"] == first["request"]["id"]
    assert await db_session.scalar(select(func.count()).select_from(CircleRequest)) == 1

    await (await authenticate(baraka)).post(f"/v1/circle/requests/{first['request']['id']}/accept")
    member = await ask(authenticate, amina, baraka)
    assert member.json() == {"relationship": "in_circle", "request": None}


async def test_you_cannot_ask_yourself_or_someone_gone(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    assert (await ask(authenticate, amina, amina)).status_code == 400
    response = await (await authenticate(amina)).post(
        "/v1/circle/requests", json={"user_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert response.status_code == 404


async def test_the_circle_needs_signing_in(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("GET", "/v1/circle"),
        ("GET", "/v1/circle/requests"),
        ("POST", "/v1/circle/requests"),
        ("GET", "/v1/feed/circle"),
        ("GET", "/v1/stories/circle"),
    ]:
        assert (await client.request(method, path)).status_code == 401, path


async def test_a_bad_direction_is_a_validation_error(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    assert (await client.get("/v1/circle/requests?direction=sideways")).status_code == 422
    assert (await client.post("/v1/circle/requests", json={"user_id": "x"})).status_code == 422


async def test_only_the_recipient_can_answer(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    outsider = await person(db_session)
    request_id = (await ask(authenticate, amina, baraka)).json()["request"]["id"]
    for who in (amina, outsider):
        for action in ("accept", "decline"):
            response = await (await authenticate(who)).post(
                f"/v1/circle/requests/{request_id}/{action}"
            )
            assert response.status_code == 404
    assert (
        await (await authenticate(baraka)).delete(f"/v1/circle/requests/{request_id}")
    ).status_code == 404


# ---------------------------------------------------------------------------
# Silent declines, withdrawals, expiry and the cooldown
# ---------------------------------------------------------------------------
async def test_a_decline_looks_like_silence_until_it_expires(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    request_id = (await ask(authenticate, amina, baraka)).json()["request"]["id"]

    declined = await (await authenticate(baraka)).post(f"/v1/circle/requests/{request_id}/decline")
    assert declined.status_code == 204
    # Nothing tells Amina: still listed, still "request_sent", no notification,
    # and asking again returns the same request as before.
    assert [item["id"] for item in await requests(authenticate, amina, "outgoing")] == [request_id]
    assert await relationship(authenticate, amina, baraka) == "request_sent"
    assert await circle_notices(db_session, amina) == []
    assert (await ask(authenticate, amina, baraka)).json()["request"]["id"] == request_id
    # Baraka no longer sees it.
    assert await requests(authenticate, baraka, "incoming") == []
    assert await relationship(authenticate, baraka, amina) == "none"

    # Thirty days on it disappears, as an unanswered one would.
    await age_request(db_session, request_id, created=31, responded=29)
    assert await requests(authenticate, amina, "outgoing") == []
    assert await relationship(authenticate, amina, baraka) == "none"


async def test_after_a_decline_the_same_person_waits_before_asking_again(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    request_id = (await ask(authenticate, amina, baraka)).json()["request"]["id"]
    await (await authenticate(baraka)).post(f"/v1/circle/requests/{request_id}/decline")
    await age_request(db_session, request_id, created=31, responded=29)

    refused = await ask(authenticate, amina, baraka)
    assert refused.status_code == 403
    assert refusal(refused) == "circle_request_not_allowed"

    # The one who declined may still ask.
    assert (await ask(authenticate, baraka, amina)).status_code == 201

    await (await authenticate(baraka)).delete(
        f"/v1/circle/requests/{(await requests(authenticate, baraka, 'outgoing'))[0]['id']}"
    )
    await age_request(db_session, request_id, created=62, responded=31)
    assert (await ask(authenticate, amina, baraka)).status_code == 201


async def test_withdrawing_a_request(db_session: AsyncSession, authenticate: Authenticate) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    request_id = (await ask(authenticate, amina, baraka)).json()["request"]["id"]

    withdrawn = await (await authenticate(amina)).delete(f"/v1/circle/requests/{request_id}")
    assert withdrawn.status_code == 204
    assert await requests(authenticate, amina, "outgoing") == []
    assert await requests(authenticate, baraka, "incoming") == []
    stored = await db_session.get(CircleRequest, request_id)
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.status == CircleRequestStatus.cancelled
    # Withdrawing starts no cooldown.
    assert (await ask(authenticate, amina, baraka)).status_code == 201


async def test_withdrawing_a_declined_request_keeps_the_cooldown(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    request_id = (await ask(authenticate, amina, baraka)).json()["request"]["id"]
    await (await authenticate(baraka)).post(f"/v1/circle/requests/{request_id}/decline")

    assert (
        await (await authenticate(amina)).delete(f"/v1/circle/requests/{request_id}")
    ).status_code == 204
    assert await requests(authenticate, amina, "outgoing") == []
    stored = await db_session.get(CircleRequest, request_id)
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.status == CircleRequestStatus.declined
    assert refusal(await ask(authenticate, amina, baraka)) == "circle_request_not_allowed"


async def test_an_unanswered_request_expires_quietly(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    request_id = (await ask(authenticate, amina, baraka)).json()["request"]["id"]
    await age_request(db_session, request_id, created=31)

    # Gone from both lists, and too late to accept, even before the worker runs.
    assert await requests(authenticate, baraka, "incoming") == []
    assert await requests(authenticate, amina, "outgoing") == []
    late = await (await authenticate(baraka)).post(f"/v1/circle/requests/{request_id}/accept")
    assert late.status_code == 404

    assert await circle_service.expire_requests(db_session, settings=settings) == 1
    stored = await db_session.get(CircleRequest, request_id)
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.status == CircleRequestStatus.expired
    assert await circle_notices(db_session, amina) == []

    # The cooldown runs from the expiry.
    assert refusal(await ask(authenticate, amina, baraka)) == "circle_request_not_allowed"
    await age_request(db_session, request_id, created=61)
    assert (await ask(authenticate, amina, baraka)).status_code == 201


async def test_a_stale_request_does_not_block_a_new_one_the_other_way(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    request_id = (await ask(authenticate, amina, baraka)).json()["request"]["id"]
    await age_request(db_session, request_id, created=31)
    # The worker has not run: the old request is still "pending" in the table.
    fresh = await ask(authenticate, baraka, amina)
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["relationship"] == "request_sent"


async def test_closed_requests_are_deleted_after_sixty_days(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    amina = await person(db_session)
    people = [await person(db_session) for _ in range(4)]
    ids = [(await ask(authenticate, amina, other)).json()["request"]["id"] for other in people]
    old_decline, recent_decline, old_expiry, waiting = ids
    for request_id in (old_decline, recent_decline):
        await (await authenticate(people[ids.index(request_id)])).post(
            f"/v1/circle/requests/{request_id}/decline"
        )
    await age_request(db_session, old_decline, created=70, responded=61)
    await age_request(db_session, recent_decline, created=70, responded=59)
    await age_request(db_session, old_expiry, created=91)
    await circle_service.expire_requests(db_session, settings=settings)

    assert await circle_service.purge_closed_requests(db_session, settings=settings) == 2
    left = {str(row) for row in await db_session.scalars(select(CircleRequest.id))}
    assert left == {recent_decline, waiting}


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("policy", "allowed"),
    [
        (CircleRequestPolicy.everyone, True),
        (CircleRequestPolicy.nobody, False),
        (CircleRequestPolicy.contacts_only, False),
        (CircleRequestPolicy.nearby_only, False),
    ],
)
async def test_a_policy_is_enforced_with_one_generic_refusal(
    db_session: AsyncSession,
    authenticate: Authenticate,
    policy: CircleRequestPolicy,
    allowed: bool,
) -> None:
    stranger = await person(db_session, at=NAIROBI)
    target = await person(db_session)
    changed = await (await authenticate(target)).patch(
        "/v1/me", json={"circle_request_policy": policy.value}
    )
    assert changed.json()["circle_request_policy"] == policy.value

    response = await ask(authenticate, stranger, target)
    if allowed:
        assert response.status_code == 201
    else:
        assert response.status_code == 403
        assert response.json()["error"]["details"] == {"reason": "circle_request_not_allowed"}
        assert await circle_notices(db_session, target) == []


async def test_nearby_only_admits_people_the_feed_reaches(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    target = await person(db_session)
    target.circle_request_policy = CircleRequestPolicy.nearby_only
    await db_session.flush()
    neighbour = await person(db_session, at=offset_km(NYERI_TOWN, east=1.0))
    assert (await ask(authenticate, neighbour, target)).status_code == 201


async def test_contacts_only_admits_someone_with_an_accepted_chat(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    target = await person(db_session)
    target.circle_request_policy = CircleRequestPolicy.contacts_only
    await db_session.flush()
    contact = await person(db_session, at=NAIROBI)
    conversation = (
        await (await authenticate(contact)).post(
            "/v1/conversations/direct", json={"user_id": uid(target)}
        )
    ).json()
    await (await authenticate(contact)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Hi, it's Kamau"}
    )
    await (await authenticate(target)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Hi Kamau!"}
    )
    assert (await ask(authenticate, contact, target)).status_code == 201


async def test_a_policy_cannot_be_set_to_something_unknown(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    assert (
        await client.patch("/v1/me", json={"circle_request_policy": "friends"})
    ).status_code == 422
    assert (await client.get("/v1/me")).json()["circle_request_policy"] == "everyone"


async def test_a_newcomer_asks_only_people_they_have_talked_to(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    newcomer = await person(db_session, contact_unlocked=False)
    stranger = await person(db_session)
    locked = await ask(authenticate, newcomer, stranger)
    assert locked.status_code == 403
    assert refusal(locked) == "contact_locked"

    neighbour = await person(db_session)
    conversation = (
        await (await authenticate(neighbour)).post(
            "/v1/conversations/direct", json={"user_id": uid(newcomer)}
        )
    ).json()
    await (await authenticate(neighbour)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Karibu!"}
    )
    await (await authenticate(newcomer)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Asante"}
    )
    assert (await ask(authenticate, newcomer, neighbour)).status_code == 201


async def test_a_newcomer_may_accept_a_request(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    newcomer = await person(db_session, contact_unlocked=False)
    neighbour = await person(db_session)
    request_id = (await ask(authenticate, neighbour, newcomer)).json()["request"]["id"]
    accepted = await (await authenticate(newcomer)).post(f"/v1/circle/requests/{request_id}/accept")
    assert accepted.status_code == 204


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
async def test_a_young_account_has_a_smaller_daily_allowance(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    young = await person(db_session, age=3)
    others = [
        await person(db_session) for _ in range(settings.new_account_circle_requests_per_day + 1)
    ]
    statuses = [(await ask(authenticate, young, other)).status_code for other in others]
    assert statuses == [201] * settings.new_account_circle_requests_per_day + [429]


async def test_too_many_waiting_requests(
    db_session: AsyncSession,
    authenticate: Authenticate,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "circle_max_pending_outgoing", 2)
    asker = await person(db_session)
    others = [await person(db_session) for _ in range(3)]
    assert (await ask(authenticate, asker, others[0])).status_code == 201
    assert (await ask(authenticate, asker, others[1])).status_code == 201
    third = await ask(authenticate, asker, others[2])
    assert third.status_code == 409
    assert refusal(third) == "too_many_pending"


async def test_a_full_circle(
    db_session: AsyncSession,
    authenticate: Authenticate,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "circle_max_members", 1)
    full = await person(db_session)
    member = await person(db_session)
    await linked(db_session, authenticate, full, member)
    outsider = await person(db_session)

    mine = await ask(authenticate, full, outsider)
    assert mine.status_code == 409
    assert refusal(mine) == "circle_full"
    # Someone asking a full circle gets the same refusal as any other.
    assert refusal(await ask(authenticate, outsider, full)) == "circle_request_not_allowed"


# ---------------------------------------------------------------------------
# Removing and blocking
# ---------------------------------------------------------------------------
async def test_removal_is_silent(db_session: AsyncSession, authenticate: Authenticate) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    await linked(db_session, authenticate, amina, baraka)
    before = len(await circle_notices(db_session, baraka))

    removed = await (await authenticate(amina)).delete(f"/v1/circle/{uid(baraka)}")
    assert removed.status_code == 204
    assert await circle(authenticate, baraka) == []
    assert len(await circle_notices(db_session, baraka)) == before
    # Again is harmless.
    assert (
        await (await authenticate(amina)).delete(f"/v1/circle/{uid(baraka)}")
    ).status_code == 204


async def test_a_block_ends_the_link_and_every_request(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    chidi = await person(db_session)
    await linked(db_session, authenticate, amina, baraka)
    await ask(authenticate, chidi, amina)

    await block_user(db_session, blocker=amina, blocked_id=baraka.id)
    await block_user(db_session, blocker=amina, blocked_id=chidi.id)
    await db_session.flush()

    assert await circle(authenticate, amina) == []
    assert await circle(authenticate, baraka) == []
    assert await requests(authenticate, chidi, "outgoing") == []
    assert await db_session.scalar(select(func.count()).select_from(CircleLink)) == 0
    assert await db_session.scalar(select(func.count()).select_from(CircleRequest)) == 0
    # And neither may ask the other: a 404, as everywhere for a block.
    assert (await ask(authenticate, baraka, amina)).status_code == 404
    assert (await ask(authenticate, amina, chidi)).status_code == 404


# ---------------------------------------------------------------------------
# Privacy: towns beyond the radius, never a distance, never a move
# ---------------------------------------------------------------------------
async def test_a_member_beyond_the_radius_shows_only_their_town(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    await make_area(
        db_session,
        name="Nairobi",
        centre=NAIROBI,
        width_km=20,
        kind="town",
        town="Nairobi",
        county="Nairobi",
    )
    amina = await person(db_session)
    near = await person(db_session, at=offset_km(NYERI_TOWN, north=1.5))
    far = await person(db_session, at=NAIROBI)
    await linked(db_session, authenticate, amina, near)
    await linked(db_session, authenticate, amina, far)

    members = {item["user"]["id"]: item for item in await circle(authenticate, amina)}
    assert members[uid(far)]["within_feed_radius"] is False
    assert members[uid(far)]["distance_bucket"] is None
    assert members[uid(far)]["area_label"] == {"label": "Nairobi", "precision": "town"}
    assert members[uid(near)]["within_feed_radius"] is True
    assert members[uid(near)]["distance_bucket"] is not None

    profile = (await (await authenticate(amina)).get(f"/v1/users/{uid(far)}")).json()
    assert profile["distance_bucket"] is None
    assert profile["area_label"] == {"label": "Nairobi", "precision": "town"}
    assert profile["posts_visible"] is True


async def test_a_stranger_beyond_the_radius_shows_only_their_town_too(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    await make_area(
        db_session,
        name="Nairobi",
        centre=NAIROBI,
        width_km=20,
        kind="town",
        town="Nairobi",
        county="Nairobi",
    )
    amina = await person(db_session)
    stranger = await person(db_session, at=NAIROBI)
    profile = (await (await authenticate(amina)).get(f"/v1/users/{uid(stranger)}")).json()
    assert profile["distance_bucket"] is None
    assert profile["area_label"]["precision"] == "town"
    assert profile["posts_visible"] is False
    assert profile["relationship"] == "none"


async def test_exploring_changes_nothing_in_the_circle(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    far = await person(db_session, at=NAIROBI)
    await linked(db_session, authenticate, amina, far)
    before = await circle(authenticate, amina)

    explored = await (await authenticate(amina)).post("/v1/me/explore", json=NAIROBI.as_payload())
    assert explored.status_code in (200, 201), explored.text
    assert await circle(authenticate, amina) == before
    # And Far's view of Amina still says she is in Nyeri, not beside them.
    theirs = {item["user"]["id"]: item for item in await circle(authenticate, far)}
    assert theirs[uid(amina)]["within_feed_radius"] is False


async def test_a_move_is_never_announced(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    await linked(db_session, authenticate, amina, baraka)
    before = (await db_session.scalar(select(func.count()).select_from(Notification))) or 0

    baraka.home_changed_at = None
    await db_session.flush()
    moved = await (await authenticate(baraka)).put("/v1/me/location", json=KARATINA.as_payload())
    assert moved.status_code == 200, moved.text
    after = (await db_session.scalar(select(func.count()).select_from(Notification))) or 0
    assert after == before


# ---------------------------------------------------------------------------
# The circle feed
# ---------------------------------------------------------------------------
async def test_the_circle_feed_brings_shared_posts_from_anywhere(
    db_session: AsyncSession, authenticate: Authenticate, redis_client: Redis
) -> None:
    await make_area(
        db_session,
        name="Nairobi",
        centre=NAIROBI,
        width_km=20,
        kind="town",
        town="Nairobi",
        county="Nairobi",
    )
    amina = await person(db_session)
    far = await person(db_session, at=NAIROBI)
    stranger = await person(db_session, at=NAIROBI)
    await linked(db_session, authenticate, amina, far)

    shared = await make_post(db_session, redis_client, author=far, body="Matatu strike in town")
    private = await (await authenticate(far)).post(
        "/v1/posts",
        json={
            "body": "Estate meeting tonight",
            "category": "general",
            "audience": "neighbours_only",
        },
    )
    assert private.status_code == 201, private.text
    assert private.json()["post"]["audience"] == "neighbours_only"
    await make_post(db_session, redis_client, author=stranger, body="Not for Amina")

    page = (await (await authenticate(amina)).get("/v1/feed/circle")).json()
    assert [item["id"] for item in page["items"]] == [uid(shared)]
    item = page["items"][0]
    assert item["distance_bucket"] is None
    assert item["area_label"] == {"label": "Nairobi", "precision": "town"}

    # The neighbourhood feed is unchanged: nothing from Nairobi.
    feed = (await (await authenticate(amina)).get("/v1/feed")).json()
    assert uid(shared) not in {post["id"] for post in feed["items"]}

    # Opening the post by link shows the same: town only, no distance.
    opened = (await (await authenticate(amina)).get(f"/v1/posts/{uid(shared)}")).json()
    assert opened["distance_bucket"] is None
    assert opened["area_label"]["precision"] == "town"


async def test_a_circle_member_sees_shared_posts_on_a_profile(
    db_session: AsyncSession, authenticate: Authenticate, redis_client: Redis, settings: Settings
) -> None:
    amina = await person(db_session)
    far = await person(db_session, at=NAIROBI)
    shared = await make_post(db_session, redis_client, author=far)
    post, _ = await feed_service.create_post(
        db_session,
        redis_client,
        author=far,
        body="Only for the estate",
        category="general",
        media_ids=[],
        settings=settings,
        audience=Audience.neighbours_only,
    )
    before = (await (await authenticate(amina)).get(f"/v1/users/{uid(far)}/posts")).json()
    assert before["items"] == []

    await linked(db_session, authenticate, amina, far)
    after = (await (await authenticate(amina)).get(f"/v1/users/{uid(far)}/posts")).json()
    assert [item["id"] for item in after["items"]] == [uid(shared)]
    assert after["items"][0]["distance_bucket"] is None
    assert uid(post) not in {item["id"] for item in after["items"]}


async def test_the_circle_feed_hides_blocked_and_removed_posts(
    db_session: AsyncSession, authenticate: Authenticate, redis_client: Redis
) -> None:
    from app.modules.moderation.models import ContentStatus

    amina = await person(db_session)
    baraka = await person(db_session, at=NAIROBI)
    chidi = await person(db_session, at=KARATINA)
    await linked(db_session, authenticate, amina, baraka)
    await linked(db_session, authenticate, amina, chidi)
    removed = await make_post(db_session, redis_client, author=chidi)
    removed.status = ContentStatus.removed
    await make_post(db_session, redis_client, author=baraka)
    await db_session.flush()

    await block_user(db_session, blocker=baraka, blocked_id=amina.id)
    await db_session.flush()
    page = (await (await authenticate(amina)).get("/v1/feed/circle")).json()
    assert page["items"] == []


async def test_circle_stories(db_session: AsyncSession, authenticate: Authenticate) -> None:
    amina = await person(db_session)
    far = await person(db_session, at=NAIROBI)
    await linked(db_session, authenticate, amina, far)
    shared = await (await authenticate(far)).post(
        "/v1/stories", json={"kind": "text", "text": "Sunset over the city"}
    )
    kept = await (await authenticate(far)).post(
        "/v1/stories", json={"kind": "text", "text": "Estate only", "audience": "neighbours_only"}
    )
    assert shared.status_code == 201 and kept.status_code == 201

    page = (await (await authenticate(amina)).get("/v1/stories/circle")).json()
    assert [item["id"] for item in page["items"]] == [shared.json()["id"]]
    assert page["items"][0]["distance_bucket"] is None
    nearby = (await (await authenticate(amina)).get("/v1/stories")).json()
    assert nearby["items"] == []


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------
async def folder(client: httpx.AsyncClient, name: str) -> list[dict[str, Any]]:
    response = await client.get(f"/v1/conversations?folder={name}")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def test_circle_members_message_without_a_request(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    far = await person(db_session, at=NAIROBI)
    await linked(db_session, authenticate, amina, far)

    conversation = (
        await (await authenticate(amina)).post(
            "/v1/conversations/direct", json={"user_id": uid(far)}
        )
    ).json()
    first = await (await authenticate(amina)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Habari ya Nairobi?"}
    )
    assert first.status_code == 201, first.text
    chats = await folder(await authenticate(far), "chats")
    assert [item["id"] for item in chats] == [conversation["id"]]
    assert await folder(await authenticate(far), "requests") == []
    request_notices = await db_session.scalar(
        select(func.count()).select_from(Notification).where(Notification.type == "message_request")
    )
    assert request_notices == 0

    # Still accepted after a removal: Amina can keep writing.
    await (await authenticate(far)).delete(f"/v1/circle/{uid(amina)}")
    again = await (await authenticate(amina)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Still here?"}
    )
    assert again.status_code == 201, again.text


async def test_a_newcomer_may_message_their_circle(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    newcomer = await person(db_session, contact_unlocked=False)
    friend = await person(db_session)
    await linked(db_session, authenticate, friend, newcomer)
    conversation = (
        await (await authenticate(newcomer)).post(
            "/v1/conversations/direct", json={"user_id": uid(friend)}
        )
    ).json()
    sent = await (await authenticate(newcomer)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Hello!"}
    )
    assert sent.status_code == 201, sent.text


async def test_a_block_still_stops_messages_in_a_circle(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    await linked(db_session, authenticate, amina, baraka)
    conversation = (
        await (await authenticate(amina)).post(
            "/v1/conversations/direct", json={"user_id": uid(baraka)}
        )
    ).json()
    await block_user(db_session, blocker=baraka, blocked_id=amina.id)
    await db_session.flush()
    refused = await (await authenticate(amina)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Hello?"}
    )
    assert refused.status_code == 404


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------
async def test_the_export_shows_a_decline_only_to_the_one_who_declined(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    amina = await person(db_session)
    baraka = await person(db_session)
    chidi = await person(db_session)
    await linked(db_session, authenticate, amina, chidi)
    request_id = (await ask(authenticate, amina, baraka)).json()["request"]["id"]
    await (await authenticate(baraka)).post(f"/v1/circle/requests/{request_id}/decline")

    mine = (await (await authenticate(amina)).get("/v1/me/export")).json()["circle"]
    assert [member["user_id"] for member in mine["members"]] == [uid(chidi)]
    sent = {item["to_user_id"]: item["status"] for item in mine["requests_sent"]}
    assert sent[uid(baraka)] == "pending"
    assert mine["who_may_ask"] == "everyone"

    theirs = (await (await authenticate(baraka)).get("/v1/me/export")).json()["circle"]
    assert [item["status"] for item in theirs["requests_received"]] == ["declined"]


async def test_the_purge_erases_the_circle(
    db_session: AsyncSession, authenticate: Authenticate, settings: Settings
) -> None:
    leaving = await person(db_session)
    friend = await person(db_session)
    other = await person(db_session)
    await linked(db_session, authenticate, leaving, friend)
    await ask(authenticate, other, leaving)
    leaving.circle_request_policy = CircleRequestPolicy.nobody
    leaving.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()

    await purge_deleted_accounts(db_session, settings=settings)
    assert await db_session.scalar(select(func.count()).select_from(CircleLink)) == 0
    assert await db_session.scalar(select(func.count()).select_from(CircleRequest)) == 0
    await db_session.refresh(leaving)
    assert leaving.circle_request_policy == CircleRequestPolicy.everyone
