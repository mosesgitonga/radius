"""Jobs, services and reviews.

The brief's "done when", as the first three tests: a provider can be found,
contacted and reviewed; reviews without a qualifying conversation are rejected;
a test scam post is held for review.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.moderation.models import AuditAction, AuditEvent
from app.modules.services.models import ServicePost
from app.modules.users.models import User
from app.modules.users.service import block_user
from tests.factories import NAIROBI, NYERI_TOWN, make_user, offset_km

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def person(db_session: AsyncSession, **kwargs: Any) -> User:
    at = kwargs.pop("at", NYERI_TOWN)
    return await make_user(db_session, at=at, created_days_ago=kwargs.pop("age", 60), **kwargs)


async def post(client: httpx.AsyncClient, expect: int = 201, **fields: Any) -> dict[str, Any]:
    body = {"kind": "offering", "category": "plumbing", "title": "Plumber, 10 years", **fields}
    response = await client.post("/v1/services", json=body)
    assert response.status_code == expect, response.text
    return dict(response.json())


async def chat_and_accept(
    authenticate: Authenticate, *, asker: User, owner: User, path: str
) -> str:
    """Open a conversation about something, and have its owner reply."""
    conversation = (await (await authenticate(asker)).post(path)).json()
    await (await authenticate(asker)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Hello, are you free?"}
    )
    await (await authenticate(owner)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Yes, Saturday works"}
    )
    return str(conversation["id"])


# ---------------------------------------------------------------------------
# The acceptance criteria
# ---------------------------------------------------------------------------
async def test_a_provider_is_found_contacted_and_reviewed(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session, display_name="Fundi Kamau")
    customer = await person(db_session, at=offset_km(NYERI_TOWN, east=3.0))
    provider_id = provider.id

    offering = await post(
        await authenticate(provider),
        title="Plumber — leaks, tanks, new fittings",
        description="Ten years fixing water tanks around Nyeri",
        rate_info="KES 800 per visit",
        travel_km=10,
    )

    # Found: a neighbour searches part of a word.
    found = (
        await (await authenticate(customer)).get("/v1/services", params={"q": "plumb tank"})
    ).json()
    assert [item["id"] for item in found["items"]] == [offering["id"]]
    assert found["items"][0]["distance_bucket"] == "3_10km"
    assert found["items"][0]["author"]["rating_count"] == 0

    # Contacted, and the provider replies — the conversation is now accepted.
    await chat_and_accept(
        authenticate, asker=customer, owner=provider, path=f"/v1/services/{offering['id']}/chat"
    )

    # Reviewed.
    review = await (await authenticate(customer)).post(
        "/v1/reviews",
        json={
            "subject_id": str(provider_id),
            "context_type": "service_post",
            "context_id": offering["id"],
            "rating": 5,
            "body": "Fixed our tank in an hour",
        },
    )
    assert review.status_code == 201, review.text
    reviews = (await (await authenticate(customer)).get(f"/v1/users/{provider_id}/reviews")).json()
    assert (reviews["rating_average"], reviews["rating_count"]) == (5.0, 1)
    assert reviews["items"][0]["body"] == "Fixed our tank in an hour"
    card = (await (await authenticate(customer)).get(f"/v1/services/{offering['id']}")).json()
    assert (card["author"]["rating_average"], card["author"]["rating_count"]) == (5.0, 1)


async def test_reviews_without_a_qualifying_conversation_are_rejected(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider = await person(db_session)
    stranger = await person(db_session)
    provider_id = provider.id
    offering = await post(await authenticate(provider))
    other_offering = await post(
        await authenticate(provider), category="electrical", title="Electrician"
    )

    async def attempt(context_id: str) -> httpx.Response:
        return await (await authenticate(stranger)).post(
            "/v1/reviews",
            json={
                "subject_id": str(provider_id),
                "context_type": "service_post",
                "context_id": context_id,
                "rating": 1,
            },
        )

    # 1. No conversation at all.
    refused = await attempt(offering["id"])
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["reason"] == "no_qualifying_conversation"

    # 2. A conversation the provider never replied to: not accepted.
    conversation = (
        await (await authenticate(stranger)).post(f"/v1/services/{offering['id']}/chat")
    ).json()
    await (await authenticate(stranger)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "You there?"}
    )
    assert (await attempt(offering["id"])).status_code == 403

    # 3. An accepted conversation, but about a different post.
    await (await authenticate(provider)).post(
        f"/v1/conversations/{conversation['id']}/messages", json={"body": "Yes"}
    )
    assert (await attempt(other_offering["id"])).status_code == 403

    # 4. A direct conversation, accepted, about nothing in particular, does not count.
    direct = (
        await (await authenticate(stranger)).post(
            "/v1/conversations/direct", json={"user_id": str(provider_id)}
        )
    ).json()
    await (await authenticate(stranger)).post(
        f"/v1/conversations/{direct['id']}/messages", json={"body": "Hi"}
    )
    await (await authenticate(provider)).post(
        f"/v1/conversations/{direct['id']}/messages", json={"body": "Hi"}
    )
    assert (await attempt(other_offering["id"])).status_code == 403

    # And with the right conversation it is allowed.
    assert (await attempt(offering["id"])).status_code == 201


async def test_a_scam_job_post_is_held_for_review(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    employer = await person(db_session)
    employer_id = employer.id
    scam = await post(
        await authenticate(employer),
        kind="hiring",
        category="house_help",
        title="House help needed urgently",
        description=(
            "Good salary. Pay a registration fee of KES 1,500 via M-Pesa to secure the job."
        ),
        job_type="full_time",
        pay_min_kes=15000,
        pay_max_kes=20000,
        pay_period="month",
    )

    assert scam["status"] == "hidden_pending_review"
    assert set(scam["held_for"]) == {"registration fee", "secure the job"}
    stored = await db_session.get(ServicePost, uuid.UUID(scam["id"]))
    assert stored is not None
    assert stored.flagged_phrases == ["registration fee", "secure the job"]
    audit = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.content_flagged)
        )
    ).scalar_one()
    assert audit.target_id == stored.id

    # Nobody else can see it, by search or by link, or contact the poster about it.
    seeker = await authenticate(await person(db_session))
    assert (await seeker.get("/v1/services", params={"kind": "hiring"})).json()["items"] == []
    assert (await seeker.get(f"/v1/services/{scam['id']}")).status_code == 404
    assert (await seeker.post(f"/v1/services/{scam['id']}/chat")).status_code == 404
    # The poster still sees it, and why.
    own = (await (await authenticate(employer)).get(f"/v1/services/{scam['id']}")).json()
    assert own["held_for"] == ["registration fee", "secure the job"]
    assert employer_id == stored.author_id


@pytest.mark.parametrize(
    "text",
    [
        "Tuma pesa ya usajili 500 kwanza",
        "Ada ya usajili ni KES 1000",
        "Lipa kwanza kabla ya kuanza kazi",
        "Training fee payable before starting",
    ],
)
async def test_fee_demands_are_caught_in_english_and_swahili(
    db_session: AsyncSession, authenticate: Authenticate, text: str
) -> None:
    job = await post(
        await authenticate(await person(db_session)),
        kind="hiring",
        category="cleaning",
        title="Cleaner wanted",
        description=text,
    )
    assert job["status"] == "hidden_pending_review"


async def test_an_honest_job_post_is_not_held(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    job = await post(
        await authenticate(await person(db_session)),
        kind="hiring",
        category="cleaning",
        title="Office cleaner, Mon to Fri",
        description="We provide uniform and transport. Kazi ni Jumatatu hadi Ijumaa.",
        job_type="part_time",
    )
    assert job["status"] == "visible"
    assert job["held_for"] is None
    assert "Never pay to get a job" in job["safety_notice"]


async def test_editing_the_phrase_out_does_not_release_a_held_post(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    job = await post(
        client, kind="hiring", category="delivery", title="Rider", description="Send money first"
    )

    edited = await client.patch(f"/v1/services/{job['id']}", json={"description": "No fees"})

    assert edited.json()["status"] == "hidden_pending_review"


async def test_editing_a_phrase_in_holds_a_live_job(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    job = await post(client, kind="hiring", category="delivery", title="Rider wanted")

    edited = await client.patch(
        f"/v1/services/{job['id']}", json={"description": "Pay to apply: KES 200"}
    )

    assert edited.json()["status"] == "hidden_pending_review"


# ---------------------------------------------------------------------------
# Job-scam guard rails
# ---------------------------------------------------------------------------
async def test_new_accounts_cannot_post_jobs(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    newcomer = await person(db_session, age=settings.hiring_min_account_age_days - 1)

    refused = await post(
        await authenticate(newcomer), expect=403, kind="hiring", category="other", title="Job"
    )

    assert refused["error"]["details"]["reason"] == "hiring_trust_threshold"
    # They can still offer or seek a service.
    await post(await authenticate(newcomer), kind="seeking", category="tutoring", title="Tutor?")


async def test_a_verified_neighbour_may_hire_whatever_their_age(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    badge = await person(db_session, age=1, is_verified_neighbour=True)
    await post(await authenticate(badge), kind="hiring", category="other", title="Helper")


async def test_a_job_conversation_carries_the_safety_notice(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    employer = await person(db_session)
    job = await post(await authenticate(employer), kind="hiring", category="catering", title="Cook")
    seeker = await person(db_session)

    opened = (await (await authenticate(seeker)).post(f"/v1/services/{job['id']}/chat")).json()

    assert "Never pay to get a job" in opened["safety_notice"]
    assert "Usilipe pesa" in opened["safety_notice"]
    # On every read, for both sides — including the employer's inbox.
    inbox = (await (await authenticate(employer)).get("/v1/conversations")).json()["items"]
    await (await authenticate(seeker)).post(
        f"/v1/conversations/{opened['id']}/messages", json={"body": "Interested"}
    )
    inbox = (await (await authenticate(employer)).get("/v1/conversations")).json()["items"]
    assert inbox[0]["safety_notice"] == opened["safety_notice"]
    # A conversation about an offering has none.
    offering = await post(await authenticate(employer), category="catering", title="Catering")
    plain = (await (await authenticate(seeker)).post(f"/v1/services/{offering['id']}/chat")).json()
    assert plain["safety_notice"] is None


async def test_job_scam_reports_come_first(db_session: AsyncSession) -> None:
    from app.modules.moderation.models import REPORT_PRIORITY, ReportReason

    assert max(REPORT_PRIORITY, key=lambda reason: REPORT_PRIORITY[reason]) in {
        ReportReason.violence,
        ReportReason.job_scam,
    }
    assert REPORT_PRIORITY[ReportReason.job_scam] > REPORT_PRIORITY[ReportReason.scam]


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------
async def test_each_kind_takes_only_its_own_fields(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))

    wrong = await post(client, expect=400, kind="seeking", title="Need a plumber", travel_km=5)
    assert wrong["error"]["details"]["fields"] == ["travel_km"]
    await post(client, expect=400, kind="offering", title="Plumber", pay_min_kes=100)


async def test_one_live_offering_per_category(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    first = await post(client)

    again = await post(client, expect=409)

    assert again["error"]["details"] == {"reason": "duplicate_offering", "post_id": first["id"]}
    await client.delete(f"/v1/services/{first['id']}")
    await post(client)


async def test_categories_come_from_configuration(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    refused = await post(
        await authenticate(await person(db_session)), expect=400, category="witchcraft"
    )
    assert "plumbing" in refused["error"]["details"]["categories"]


async def test_browse_filters_by_kind_category_and_distance(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    plumber = await post(await authenticate(await person(db_session)))
    tutor = await post(
        await authenticate(await person(db_session)),
        kind="seeking",
        category="tutoring",
        title="Maths tutor needed",
    )
    await post(await authenticate(await person(db_session, at=NAIROBI)), title="Far plumber")
    client = await authenticate(await person(db_session))

    everything = (await client.get("/v1/services")).json()
    assert {item["id"] for item in everything["items"]} == {plumber["id"], tutor["id"]}
    assert 3 <= everything["radius_km"] <= 40
    seeking = (await client.get("/v1/services", params={"kind": "seeking"})).json()
    assert [item["id"] for item in seeking["items"]] == [tutor["id"]]
    tutoring = (await client.get("/v1/services", params={"category": "tutoring"})).json()
    assert [item["id"] for item in tutoring["items"]] == [tutor["id"]]


async def test_nearest_sorts_by_band(db_session: AsyncSession, authenticate: Authenticate) -> None:
    far = await post(
        await authenticate(await person(db_session, at=offset_km(NYERI_TOWN, north=5))),
        title="Far fundi",
    )
    near = await post(await authenticate(await person(db_session)), title="Near fundi")
    client = await authenticate(await person(db_session))

    found = (await client.get("/v1/services", params={"sort": "nearest"})).json()

    assert [item["id"] for item in found["items"]] == [near["id"], far["id"]]
    assert [item["distance_bucket"] for item in found["items"]] == ["under_1km", "3_10km"]


async def test_closed_and_expired_posts_leave_search(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    from sqlalchemy import update

    author = await person(db_session)
    closed = await post(
        await authenticate(author), kind="seeking", category="laundry", title="Mama fua"
    )
    expired = await post(
        await authenticate(author), kind="seeking", category="cleaning", title="Cleaner"
    )
    await (await authenticate(author)).put(
        f"/v1/services/{closed['id']}/open", json={"open": False}
    )
    await db_session.execute(
        update(ServicePost)
        .where(ServicePost.id == uuid.UUID(expired["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(days=1))
    )
    await db_session.commit()
    client = await authenticate(await person(db_session))

    assert (await client.get("/v1/services")).json()["items"] == []
    assert (await client.get(f"/v1/services/{closed['id']}")).json()["is_open"] is False
    assert (await client.post(f"/v1/services/{closed['id']}/chat")).status_code == 409

    reopened = await (await authenticate(author)).put(
        f"/v1/services/{closed['id']}/open", json={"open": True}
    )
    assert reopened.json()["is_open"] is True


async def test_blocked_authors_are_left_out(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    author, viewer = await person(db_session), await person(db_session)
    offering = await post(await authenticate(author))
    await block_user(db_session, blocker=viewer, blocked_id=author.id)

    client = await authenticate(viewer)
    assert (await client.get("/v1/services")).json()["items"] == []
    assert (await client.get(f"/v1/services/{offering['id']}")).status_code == 404


async def test_only_the_author_changes_a_post(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    offering = await post(await authenticate(await person(db_session)))
    other = await authenticate(await person(db_session))

    for method, path, body in (
        ("PATCH", f"/v1/services/{offering['id']}", {"title": "Mine"}),
        ("PUT", f"/v1/services/{offering['id']}/open", {"open": False}),
        ("DELETE", f"/v1/services/{offering['id']}", None),
    ):
        assert (await other.request(method, path, json=body)).status_code == 403


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------
async def _reviewed(
    db_session: AsyncSession, authenticate: Authenticate, rating: int = 4
) -> tuple[User, User, dict[str, Any], dict[str, Any]]:
    provider, customer = await person(db_session), await person(db_session)
    offering = await post(await authenticate(provider))
    await chat_and_accept(
        authenticate, asker=customer, owner=provider, path=f"/v1/services/{offering['id']}/chat"
    )
    review = await (await authenticate(customer)).post(
        "/v1/reviews",
        json={
            "subject_id": str(provider.id),
            "context_type": "service_post",
            "context_id": offering["id"],
            "rating": rating,
            "body": "Good work",
        },
    )
    assert review.status_code == 201, review.text
    return provider, customer, offering, review.json()


async def test_one_review_per_pair_per_context(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider, customer, offering, first = await _reviewed(db_session, authenticate)

    again = await (await authenticate(customer)).post(
        "/v1/reviews",
        json={
            "subject_id": str(provider.id),
            "context_type": "service_post",
            "context_id": offering["id"],
            "rating": 1,
        },
    )

    assert again.status_code == 409
    assert again.json()["error"]["details"]["review_id"] == first["id"]


async def test_the_provider_may_review_back(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider, customer, offering, _review = await _reviewed(db_session, authenticate)

    back = await (await authenticate(provider)).post(
        "/v1/reviews",
        json={
            "subject_id": str(customer.id),
            "context_type": "service_post",
            "context_id": offering["id"],
            "rating": 5,
            "body": "Paid on time",
        },
    )

    assert back.status_code == 201


async def test_ratings_follow_edits_and_deletions(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider, customer, _offering, review = await _reviewed(db_session, authenticate, rating=2)
    provider_id = provider.id
    client = await authenticate(customer)

    await client.patch(f"/v1/reviews/{review['id']}", json={"rating": 4})
    rated = (await client.get(f"/v1/users/{provider_id}/reviews")).json()
    assert (rated["rating_average"], rated["rating_count"]) == (4.0, 1)

    await client.delete(f"/v1/reviews/{review['id']}")
    cleared = (await client.get(f"/v1/users/{provider_id}/reviews")).json()
    assert (cleared["rating_average"], cleared["rating_count"]) == (None, 0)


async def test_the_reviewed_person_can_reply_once(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider, customer, _offering, review = await _reviewed(db_session, authenticate)

    replied = await (await authenticate(provider)).put(
        f"/v1/reviews/{review['id']}/reply", json={"body": "Asante sana!"}
    )
    assert replied.json()["reply_body"] == "Asante sana!"
    not_theirs = await (await authenticate(customer)).put(
        f"/v1/reviews/{review['id']}/reply", json={"body": "Me too"}
    )
    assert not_theirs.status_code == 403
    edited = await (await authenticate(provider)).put(
        f"/v1/reviews/{review['id']}/reply", json={"body": "Asante!"}
    )
    assert edited.json()["reply_body"] == "Asante!"


async def test_a_listing_buyer_can_review_the_seller(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller, buyer = await person(db_session), await person(db_session)
    seller_id = seller.id
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
    await chat_and_accept(
        authenticate, asker=buyer, owner=seller, path=f"/v1/listings/{listing['id']}/chat"
    )

    review = await (await authenticate(buyer)).post(
        "/v1/reviews",
        json={
            "subject_id": str(seller_id),
            "context_type": "listing",
            "context_id": listing["id"],
            "rating": 4,
            "body": "As described",
        },
    )

    assert review.status_code == 201


async def test_reported_reviews_are_hidden_and_stop_counting(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    provider, _customer, _offering, review = await _reviewed(db_session, authenticate, rating=1)
    provider_id = provider.id

    for _ in range(settings.report_auto_hide_threshold):
        reporter = await person(db_session, age=90)
        response = await (await authenticate(reporter)).post(
            "/v1/reports",
            json={"target_type": "review", "target_id": review["id"], "reason": "harassment"},
        )
        assert response.status_code == 201

    viewer = await authenticate(await person(db_session))
    reviews = (await viewer.get(f"/v1/users/{provider_id}/reviews")).json()
    assert reviews["items"] == []
    assert (reviews["rating_average"], reviews["rating_count"]) == (None, 0)


async def test_reviews_by_blocked_people_are_left_out(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    provider, customer, _offering, _review = await _reviewed(db_session, authenticate)
    viewer = await person(db_session)
    await block_user(db_session, blocker=viewer, blocked_id=customer.id)

    seen = (await (await authenticate(viewer)).get(f"/v1/users/{provider.id}/reviews")).json()

    assert seen["items"] == []
    # The rating itself still counts the review: blocking is about what you see.
    assert seen["rating_count"] == 1


async def test_export_and_purge(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    from app.modules.users.tasks import purge_deleted_accounts

    provider, customer, offering, review = await _reviewed(db_session, authenticate, rating=5)
    provider_id = provider.id

    export = (await (await authenticate(customer)).get("/v1/me/export")).json()
    assert [item["id"] for item in export["reviews"]["written"]] == [review["id"]]
    provider_export = (await (await authenticate(provider)).get("/v1/me/export")).json()
    assert [item["id"] for item in provider_export["services"]] == [offering["id"]]
    assert [item["id"] for item in provider_export["reviews"]["received"]] == [review["id"]]

    await db_session.refresh(customer)
    customer.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()
    await purge_deleted_accounts(db_session, settings=settings)

    rated = (await (await authenticate(provider)).get(f"/v1/users/{provider_id}/reviews")).json()
    assert (rated["rating_average"], rated["rating_count"], rated["items"]) == (None, 0, [])
