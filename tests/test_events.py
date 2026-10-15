"""Events. The brief's "done when", first:

* reminders fire at the right local times — the sweep is run at chosen instants
  around a Saturday 18:00 event in Nairobi, and must send each reminder exactly
  when it is due, worded in local time;
* private-event venues are hidden from non-attendees — in every response that
  carries the event, not only the detail page.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import run_after_commit
from app.core.queue import RecordingJobQueue
from app.modules.events import service as events_service
from app.modules.events import times
from app.modules.notifications.models import Notification
from app.modules.users.models import User
from app.modules.users.service import block_user
from tests.factories import NAIROBI, NYERI_TOWN, make_user, offset_km

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]
NAIROBI_TZ = ZoneInfo("Africa/Nairobi")
VENUE = {"latitude": -0.4205, "longitude": 36.9480}
SECRET_VENUE = "Mama Njeri's house, gate 14, Kamakwa"


def next_saturday_six_pm(weeks_ahead: int = 1) -> datetime:
    """A Saturday 18:00 in Nairobi at least a week away, so the test never depends
    on what day it is run."""
    today = datetime.now(NAIROBI_TZ).date()
    days = (5 - today.weekday()) % 7 + 7 * weeks_ahead
    return datetime.combine(today + timedelta(days=days), time(18, 0), tzinfo=NAIROBI_TZ)


async def person(db_session: AsyncSession, **kwargs: Any) -> User:
    return await make_user(
        db_session,
        at=kwargs.pop("at", NYERI_TOWN),
        created_days_ago=kwargs.pop("age", 30),
        **kwargs,
    )


async def organise(client: httpx.AsyncClient, expect: int = 201, **fields: Any) -> dict[str, Any]:
    start = fields.pop("start", next_saturday_six_pm())
    body = {
        "title": "Kamakwa harambee",
        "event_type": "harambee",
        # No offset: local wall time, as a person would type it.
        "start_at": start.replace(tzinfo=None).isoformat(),
        "end_at": (start + timedelta(hours=3)).replace(tzinfo=None).isoformat(),
        "venue": VENUE,
        "venue_name": "Kamakwa social hall",
        "venue_details": "Behind the chief's office",
        "visibility": "public",
        **fields,
    }
    response = await client.post("/v1/events", json=body)
    assert response.status_code == expect, response.text
    return dict(response.json())


async def reply(client: httpx.AsyncClient, event_id: str, response: str) -> httpx.Response:
    return await client.put(f"/v1/events/{event_id}/rsvp", json={"response": response})


async def reminders_for(db_session: AsyncSession, user: User) -> list[Notification]:
    return list(
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == user.id, Notification.type == "event_reminder"
                )
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Acceptance: reminders at the right local times
# ---------------------------------------------------------------------------
async def test_reminders_fire_at_the_right_local_times(
    db_session: AsyncSession,
    authenticate: Authenticate,
    settings: Any,
    queue: RecordingJobQueue,
) -> None:
    organiser = await person(db_session)
    going, interested, not_going = (
        await person(db_session),
        await person(db_session),
        await person(db_session),
    )
    start_local = next_saturday_six_pm()
    event = await organise(await authenticate(organiser), start=start_local)
    assert event["start_local"].startswith(start_local.strftime("%Y-%m-%dT18:00:00"))
    assert event["start_local"].endswith("+03:00")
    # 18:00 in Nairobi is 15:00 UTC.
    assert event["start_at"].startswith(start_local.astimezone(UTC).strftime("%Y-%m-%dT15:00:00"))
    await reply(await authenticate(going), event["id"], "going")
    await reply(await authenticate(interested), event["id"], "interested")
    await reply(await authenticate(not_going), event["id"], "not_going")
    queue.clear()

    async def sweep_at(local: datetime) -> int:
        run = await events_service.send_event_reminders(
            db_session, settings=settings, now=local.astimezone(UTC)
        )
        await db_session.flush()
        # What the worker's session does on commit: run the queued pushes.
        await run_after_commit(db_session)
        return run.sent

    friday = start_local - timedelta(days=1)
    # A minute early: nothing.
    assert await sweep_at(friday - timedelta(minutes=1)) == 0
    # Friday 18:00 local, a day before: the organiser and the one attendee going.
    assert await sweep_at(friday) == 2
    assert [n.data["offset_minutes"] for n in await reminders_for(db_session, going)] == ["1440"]
    assert await reminders_for(db_session, interested) == []
    assert await reminders_for(db_session, not_going) == []
    # Worded in local time.
    texts = [
        args[4]["push_text"] for name, args, _ in queue.jobs if name == "deliver_notification_push"
    ]
    assert texts == ["Kamakwa harambee starts tomorrow at 18:00"] * 2

    # Run again half an hour later: nothing new — each reminder is sent once.
    assert await sweep_at(friday + timedelta(minutes=30)) == 0
    # Saturday 16:59 local: still nothing; 17:00, an hour before: the second.
    assert await sweep_at(start_local - timedelta(minutes=61)) == 0
    queue.clear()
    assert await sweep_at(start_local - timedelta(hours=1)) == 2
    texts = [
        args[4]["push_text"] for name, args, _ in queue.jobs if name == "deliver_notification_push"
    ]
    assert texts == ["Kamakwa harambee starts today at 18:00"] * 2
    # And none once it has started.
    assert await sweep_at(start_local + timedelta(minutes=1)) == 0
    assert sorted(n.data["offset_minutes"] for n in await reminders_for(db_session, going)) == [
        "1440",
        "60",
    ]


async def test_a_late_rsvp_gets_the_next_reminder_not_a_stale_one(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    from sqlalchemy import update

    from app.modules.events.models import EventRsvp

    organiser, latecomer = await person(db_session), await person(db_session)
    latecomer_id = latecomer.id
    start_local = next_saturday_six_pm()
    event = await organise(await authenticate(organiser), start=start_local)
    await reply(await authenticate(latecomer), event["id"], "going")
    # They replied three hours before it starts.
    await db_session.execute(
        update(EventRsvp)
        .where(EventRsvp.user_id == latecomer_id)
        .values(updated_at=(start_local - timedelta(hours=3)).astimezone(UTC))
    )
    await db_session.flush()

    # The day-before reminder is still due three hours before — but only to the
    # organiser, who was going all along.
    three_hours_before = (start_local - timedelta(hours=3)).astimezone(UTC)
    run = await events_service.send_event_reminders(
        db_session, settings=settings, now=three_hours_before
    )
    await db_session.flush()
    assert run.sent == 1
    assert await reminders_for(db_session, latecomer) == []

    one_hour_before = (start_local - timedelta(hours=1)).astimezone(UTC)
    await events_service.send_event_reminders(db_session, settings=settings, now=one_hour_before)
    await db_session.flush()
    assert [n.data["offset_minutes"] for n in await reminders_for(db_session, latecomer)] == ["60"]


async def test_moving_an_event_re_times_its_reminders(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    organiser = await person(db_session)
    start_local = next_saturday_six_pm()
    event = await organise(await authenticate(organiser), start=start_local)
    friday = (start_local - timedelta(days=1)).astimezone(UTC)
    assert (
        await events_service.send_event_reminders(db_session, settings=settings, now=friday)
    ).sent == 1

    later = start_local + timedelta(days=7)
    moved = await (await authenticate(organiser)).patch(
        f"/v1/events/{event['id']}",
        json={
            "start_at": later.replace(tzinfo=None).isoformat(),
            "end_at": (later + timedelta(hours=3)).replace(tzinfo=None).isoformat(),
        },
    )
    assert moved.status_code == 200, moved.text

    new_friday = (later - timedelta(days=1)).astimezone(UTC)
    assert (
        await events_service.send_event_reminders(db_session, settings=settings, now=new_friday)
    ).sent == 1


async def test_a_cancelled_event_sends_no_reminders(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    organiser = await person(db_session)
    start_local = next_saturday_six_pm()
    event = await organise(await authenticate(organiser), start=start_local)
    await (await authenticate(organiser)).post(f"/v1/events/{event['id']}/cancel", json={})

    friday = (start_local - timedelta(days=1)).astimezone(UTC)
    assert (
        await events_service.send_event_reminders(db_session, settings=settings, now=friday)
    ).sent == 0


@pytest.mark.parametrize(
    ("now_local", "start_local", "expected"),
    [
        ("2026-10-23T18:00", "2026-10-24T18:00", "tomorrow at 18:00"),
        ("2026-10-24T17:00", "2026-10-24T18:00", "today at 18:00"),
        ("2026-10-20T09:00", "2026-10-24T09:30", "on Sat 24 Oct at 09:30"),
        # 23:30 local on Friday is 20:30 UTC: the local date decides "tomorrow".
        ("2026-10-23T23:30", "2026-10-24T00:30", "tomorrow at 00:30"),
    ],
)
def test_reminder_wording_uses_local_dates(now_local: str, start_local: str, expected: str) -> None:
    now = datetime.fromisoformat(now_local).replace(tzinfo=NAIROBI_TZ)
    start = datetime.fromisoformat(start_local).replace(tzinfo=NAIROBI_TZ)
    assert times.describe_start(start, now=now, zone=NAIROBI_TZ) == expected


# ---------------------------------------------------------------------------
# Acceptance: private venues hidden from non-attendees
# ---------------------------------------------------------------------------
async def test_a_private_events_venue_is_hidden_from_non_attendees(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    organiser = await person(db_session)
    invitee, outsider = await person(db_session), await person(db_session)
    invitee_id = invitee.id
    event = await organise(
        await authenticate(organiser),
        title="Wanjiku's birthday",
        event_type="party",
        visibility="private",
        venue_name=SECRET_VENUE,
        venue_details="Blue gate after the kiosk",
    )
    assert event["venue_name"] == SECRET_VENUE  # the organiser sees it

    # An outsider cannot see the event at all.
    outside = await authenticate(outsider)
    response = await outside.get(f"/v1/events/{event['id']}")
    assert response.status_code == 404
    assert SECRET_VENUE not in response.text

    # Invited, not yet answered: the event and its area, but not the venue.
    await (await authenticate(organiser)).post(
        f"/v1/events/{event['id']}/invites", json={"user_ids": [str(invitee_id)]}
    )
    client = await authenticate(invitee)
    seen = await client.get(f"/v1/events/{event['id']}")
    assert seen.status_code == 200
    body = seen.json()
    assert (body["venue_visible"], body["venue_name"], body["venue_details"]) == (False, None, None)
    assert body["area_label"]["label"]
    assert SECRET_VENUE not in seen.text and "Blue gate" not in seen.text

    # Saying no keeps it hidden; saying going reveals it.
    declined = await reply(client, event["id"], "not_going")
    assert declined.json()["venue_name"] is None
    accepted = await reply(client, event["id"], "going")
    assert accepted.json()["venue_name"] == SECRET_VENUE
    assert accepted.json()["venue_details"] == "Blue gate after the kiosk"


async def test_an_unapproved_reply_does_not_reveal_the_venue(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    organiser, guest = await person(db_session), await person(db_session)
    guest_id = guest.id
    event = await organise(
        await authenticate(organiser),
        visibility="private",
        requires_approval=True,
        venue_name=SECRET_VENUE,
    )
    token = (await (await authenticate(organiser)).post(f"/v1/events/{event['id']}/link")).json()[
        "token"
    ]

    client = await authenticate(guest)
    preview = await client.get(f"/v1/events/invite/{token}")
    assert preview.json()["venue_name"] is None and SECRET_VENUE not in preview.text
    asked = await client.post(f"/v1/events/invite/{token}/rsvp", json={"response": "going"})
    assert asked.json()["my_rsvp"] == {"response": "going", "approval": "pending"}
    assert asked.json()["venue_name"] is None
    # Not counted until approved.
    assert asked.json()["going_count"] == 1  # the organiser

    approved = await (await authenticate(organiser)).post(
        f"/v1/events/{event['id']}/attendees/{guest_id}/review", json={"action": "approve"}
    )
    assert approved.status_code == 204
    now_seen = (await (await authenticate(guest)).get(f"/v1/events/{event['id']}")).json()
    assert now_seen["venue_name"] == SECRET_VENUE
    assert now_seen["going_count"] == 2


async def test_the_venue_stays_hidden_in_every_list(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    organiser, invitee = await person(db_session), await person(db_session)
    event = await organise(
        await authenticate(organiser), visibility="private", venue_name=SECRET_VENUE
    )
    await (await authenticate(organiser)).post(
        f"/v1/events/{event['id']}/invites", json={"user_ids": [str(invitee.id)]}
    )
    await reply(await authenticate(invitee), event["id"], "not_going")
    # Even then it would not appear in browse — private events are never discovered.
    client = await authenticate(invitee)
    for path in ("/v1/events", "/v1/events/mine?organising=false", "/v1/notifications"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert SECRET_VENUE not in response.text, path


async def test_a_bad_or_replaced_link_is_a_404(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    organiser = await person(db_session)
    event = await organise(await authenticate(organiser), visibility="private")
    first = (await (await authenticate(organiser)).post(f"/v1/events/{event['id']}/link")).json()[
        "token"
    ]
    await (await authenticate(organiser)).post(f"/v1/events/{event['id']}/link")

    client = await authenticate(await person(db_session))
    assert (await client.get(f"/v1/events/invite/{first}")).status_code == 404
    assert (await client.get("/v1/events/invite/nonsense")).status_code == 404


# ---------------------------------------------------------------------------
# Visibility and browse
# ---------------------------------------------------------------------------
async def test_browse_finds_public_events_nearby_only(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    near = await organise(
        await authenticate(await person(db_session)), title="Near party", event_type="party"
    )
    await organise(
        await authenticate(await person(db_session)), title="Secret", visibility="private"
    )
    await organise(
        await authenticate(await person(db_session, at=NAIROBI)),
        title="Far away",
        venue={"latitude": NAIROBI.latitude, "longitude": NAIROBI.longitude},
    )
    client = await authenticate(await person(db_session, at=offset_km(NYERI_TOWN, north=2)))

    found = (await client.get("/v1/events")).json()

    assert [item["id"] for item in found["items"]] == [near["id"]]
    assert found["items"][0]["venue_name"] == "Kamakwa social hall"
    assert found["items"][0]["timezone"] == "Africa/Nairobi"
    assert (await client.get("/v1/events", params={"event_type": "church"})).json()["items"] == []


async def test_today_and_this_weekend_are_local(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    now_local = datetime.now(NAIROBI_TZ)
    later_today = now_local + timedelta(minutes=30)
    if later_today.date() != now_local.date():
        pytest.skip("Too close to local midnight to have an event later today")
    today = await organise(client, title="Today", start=later_today)
    weekend = await organise(
        client, title="Weekend", start=next_saturday_six_pm(weeks_ahead=0) + timedelta(days=0)
    )
    much_later = await organise(
        client, title="Next month", start=next_saturday_six_pm(weeks_ahead=4)
    )
    viewer = await authenticate(await person(db_session))

    on_today = {
        item["id"]
        for item in (await viewer.get("/v1/events", params={"when": "today"})).json()["items"]
    }
    on_weekend = {
        item["id"]
        for item in (await viewer.get("/v1/events", params={"when": "this_weekend"})).json()[
            "items"
        ]
    }
    upcoming = [item["id"] for item in (await viewer.get("/v1/events")).json()["items"]]

    assert today["id"] in on_today and much_later["id"] not in on_today
    assert weekend["id"] in on_weekend and much_later["id"] not in on_weekend
    assert upcoming.index(today["id"]) < upcoming.index(much_later["id"])


def test_the_weekend_window() -> None:
    wednesday = datetime(2026, 10, 21, 12, tzinfo=NAIROBI_TZ)
    start, end = times.window("this_weekend", now=wednesday.astimezone(UTC), zone=NAIROBI_TZ)
    assert start == datetime(2026, 10, 24, 0, tzinfo=NAIROBI_TZ)
    assert end == datetime(2026, 10, 26, 0, tzinfo=NAIROBI_TZ)
    sunday = datetime(2026, 10, 25, 15, tzinfo=NAIROBI_TZ)
    start, end = times.window("this_weekend", now=sunday.astimezone(UTC), zone=NAIROBI_TZ)
    assert start == sunday and end == datetime(2026, 10, 26, 0, tzinfo=NAIROBI_TZ)


async def test_group_only_events_are_for_members(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    admin, member, outsider = (
        await person(db_session),
        await person(db_session),
        await person(db_session),
    )
    group = (
        await (await authenticate(admin)).post(
            "/v1/groups",
            json={"name": "Kamakwa residents", "category": "estate", "visibility": "public"},
        )
    ).json()
    await (await authenticate(member)).post(f"/v1/groups/{group['id']}/join")
    event = await organise(await authenticate(admin), visibility="group", group_id=group["id"])

    member_view = await (await authenticate(member)).get(f"/v1/events/{event['id']}")
    assert member_view.status_code == 200
    group_list = (await (await authenticate(member)).get(f"/v1/groups/{group['id']}/events")).json()
    assert [item["id"] for item in group_list["items"]] == [event["id"]]
    outside = await authenticate(outsider)
    assert (await outside.get(f"/v1/events/{event['id']}")).status_code == 404
    assert (await outside.get(f"/v1/groups/{group['id']}/events")).json()["items"] == []
    # Only a group admin can organise for the group.
    await organise(await authenticate(member), expect=403, visibility="group", group_id=group["id"])


async def test_blocked_organisers_are_hidden(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    organiser, viewer = await person(db_session), await person(db_session)
    event = await organise(await authenticate(organiser))
    await block_user(db_session, blocker=viewer, blocked_id=organiser.id)

    client = await authenticate(viewer)
    assert (await client.get("/v1/events")).json()["items"] == []
    assert (await client.get(f"/v1/events/{event['id']}")).status_code == 404


# ---------------------------------------------------------------------------
# RSVPs
# ---------------------------------------------------------------------------
async def test_capacity_limits_going(db_session: AsyncSession, authenticate: Authenticate) -> None:
    event = await organise(await authenticate(await person(db_session)), capacity=2)
    first, second = await person(db_session), await person(db_session)

    assert (await reply(await authenticate(first), event["id"], "going")).json()["is_full"] is True
    full = await reply(await authenticate(second), event["id"], "going")
    assert full.status_code == 409
    # Interest is unlimited, and a place frees up when someone pulls out.
    assert (await reply(await authenticate(second), event["id"], "interested")).status_code == 200
    await reply(await authenticate(first), event["id"], "not_going")
    assert (await reply(await authenticate(second), event["id"], "going")).status_code == 200


async def test_attendee_list_visibility(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    organiser, attendee, onlooker = (
        await person(db_session),
        await person(db_session),
        await person(db_session),
    )
    event = await organise(await authenticate(organiser), attendee_visibility="attendees")
    await reply(await authenticate(attendee), event["id"], "going")

    assert (
        await (await authenticate(onlooker)).get(f"/v1/events/{event['id']}/attendees")
    ).status_code == 403
    seen = (await (await authenticate(attendee)).get(f"/v1/events/{event['id']}/attendees")).json()
    assert len(seen["items"]) == 2

    await (await authenticate(organiser)).patch(
        f"/v1/events/{event['id']}", json={"attendee_visibility": "organiser"}
    )
    assert (
        await (await authenticate(attendee)).get(f"/v1/events/{event['id']}/attendees")
    ).status_code == 403
    assert (
        await (await authenticate(organiser)).get(f"/v1/events/{event['id']}/attendees")
    ).status_code == 200


async def test_no_replies_to_cancelled_or_finished_events(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    organiser = await person(db_session)
    event = await organise(await authenticate(organiser))
    await (await authenticate(organiser)).post(
        f"/v1/events/{event['id']}/cancel", json={"reason": "Rain"}
    )

    response = await reply(await authenticate(await person(db_session)), event["id"], "going")

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Changes and cancellations notify
# ---------------------------------------------------------------------------
async def test_changes_and_cancellations_notify_everyone_who_replied(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    organiser = await person(db_session)
    going, interested, declined = (
        await person(db_session),
        await person(db_session),
        await person(db_session),
    )
    bystander = await person(db_session)
    event = await organise(await authenticate(organiser))
    for user, response in ((going, "going"), (interested, "interested"), (declined, "not_going")):
        await reply(await authenticate(user), event["id"], response)

    async def kinds(user: User) -> list[str]:
        return sorted(
            (
                await db_session.execute(
                    select(Notification.type).where(Notification.user_id == user.id)
                )
            ).scalars()
        )

    # A reworded description tells nobody.
    await (await authenticate(organiser)).patch(
        f"/v1/events/{event['id']}", json={"description": "Bring a jembe"}
    )
    assert await kinds(going) == []
    # A new venue tells everyone who replied.
    await (await authenticate(organiser)).patch(
        f"/v1/events/{event['id']}", json={"venue_name": "Kamakwa primary school"}
    )
    for user in (going, interested, declined):
        assert await kinds(user) == ["event_updated"]
    assert await kinds(bystander) == []

    await (await authenticate(organiser)).post(
        f"/v1/events/{event['id']}/cancel", json={"reason": "Rain"}
    )
    for user in (going, interested, declined):
        assert await kinds(user) == ["event_cancelled", "event_updated"]
    cancelled = (await (await authenticate(going)).get(f"/v1/events/{event['id']}")).json()
    assert (cancelled["event_status"], cancelled["cancellation_reason"]) == ("cancelled", "Rain")


# ---------------------------------------------------------------------------
# Validation, carried-over reminders, data rights
# ---------------------------------------------------------------------------
async def test_event_times_are_validated(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    start = next_saturday_six_pm()
    backwards = await client.post(
        "/v1/events",
        json={
            "title": "x",
            "event_type": "other",
            "start_at": start.isoformat(),
            "end_at": (start - timedelta(hours=1)).isoformat(),
            "venue": VENUE,
            "venue_name": "Here",
            "visibility": "public",
        },
    )
    assert backwards.status_code == 400
    past = datetime.now(NAIROBI_TZ) - timedelta(days=2)
    await organise(client, expect=400, start=past)


async def test_coordinates_never_leave_or_stay(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    from app.modules.events.models import Event

    event = await organise(await authenticate(await person(db_session)))
    stored = await db_session.get(Event, uuid.UUID(event["id"]))
    assert stored is not None
    assert not {"latitude", "longitude"} & {column.name for column in Event.__table__.columns}
    assert "36.948" not in str(event) and "-0.4205" not in str(event)


async def test_a_seller_is_reminded_before_a_listing_expires(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    from sqlalchemy import update

    from app.modules.marketplace.models import Listing

    seller = await person(db_session)
    listing = (
        await (await authenticate(seller)).post(
            "/v1/listings",
            json={
                "title": "Old radio",
                "category": "electronics",
                "condition": "fair",
                "pricing": "fixed",
                "price_kes": 900,
            },
        )
    ).json()
    soon = datetime.now(UTC) + timedelta(hours=settings.listing_expiry_reminder_hours - 1)
    await db_session.execute(
        update(Listing).where(Listing.id == uuid.UUID(listing["id"])).values(expires_at=soon)
    )
    await db_session.flush()

    assert await events_service.remind_expiring_listings(db_session, settings=settings) == 1
    assert await events_service.remind_expiring_listings(db_session, settings=settings) == 0
    kinds = (
        (
            await db_session.execute(
                select(Notification.type).where(Notification.user_id == seller.id)
            )
        )
        .scalars()
        .all()
    )
    assert list(kinds) == ["listing_expiring"]


async def test_a_named_buyer_is_invited_to_review(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller, buyer = await person(db_session), await person(db_session)
    buyer_id = buyer.id
    listing = (
        await (await authenticate(seller)).post(
            "/v1/listings",
            json={
                "title": "Bike",
                "category": "sports",
                "condition": "good",
                "pricing": "fixed",
                "price_kes": 5000,
            },
        )
    ).json()
    chat = (await (await authenticate(buyer)).post(f"/v1/listings/{listing['id']}/chat")).json()
    await (await authenticate(buyer)).post(
        f"/v1/conversations/{chat['id']}/messages", json={"body": "Mine?"}
    )

    await (await authenticate(seller)).put(
        f"/v1/listings/{listing['id']}/availability",
        json={"availability": "sold", "buyer_id": str(buyer_id)},
    )

    prompt = (
        await db_session.execute(
            select(Notification).where(
                Notification.user_id == buyer_id, Notification.type == "review_prompt"
            )
        )
    ).scalar_one()
    assert prompt.data["listing_id"] == listing["id"]


async def test_export_and_purge(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    from app.modules.users.tasks import purge_deleted_accounts

    organiser, guest = await person(db_session), await person(db_session)
    guest_id = guest.id
    event = await organise(await authenticate(organiser))
    await reply(await authenticate(guest), event["id"], "going")

    export = (await (await authenticate(organiser)).get("/v1/me/export")).json()
    assert [item["id"] for item in export["events"]["organised"]] == [event["id"]]

    await db_session.refresh(organiser)
    organiser.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()
    await purge_deleted_accounts(db_session, settings=settings)

    assert (await (await authenticate(guest)).get(f"/v1/events/{event['id']}")).status_code == 404
    told = (
        (
            await db_session.execute(
                select(Notification.type).where(Notification.user_id == guest_id)
            )
        )
        .scalars()
        .all()
    )
    assert "event_cancelled" in told


@pytest.mark.parametrize(
    ("minutes_before", "expected"),
    [(1500, None), (1440, 1440), (300, 1440), (60, 60), (30, 60), (0, None), (-10, None)],
)
def test_which_reminder_is_due(minutes_before: int, expected: int | None) -> None:
    """The rule the sweep's windows implement: each offset from its moment until
    the next, smaller one; nothing once the event has started."""
    start = datetime(2026, 10, 24, 15, tzinfo=UTC)
    now = start - timedelta(minutes=minutes_before)
    assert times.reminder_due((1440, 60), start_at=start, now=now) == expected
