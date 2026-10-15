"""The marketplace: listing, finding, asking, selling — and the anti-abuse rules.

The first test is the brief's "done when", start to finish: a user lists an item,
a nearby buyer finds it by search, chats with the seller, and the seller marks it
sold.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.storage import MemoryStorageProvider
from app.modules.marketplace.models import Listing
from app.modules.media.models import MediaPurpose
from app.modules.notifications.models import Notification
from app.modules.users.models import User
from app.modules.users.service import block_user
from tests.factories import KARATINA, NAIROBI, NYERI_TOWN, make_ready_image, make_user, offset_km

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def person(db_session: AsyncSession, **kwargs: Any) -> User:
    at = kwargs.pop("at", NYERI_TOWN)
    return await make_user(db_session, at=at, created_days_ago=kwargs.pop("age", 30), **kwargs)


async def list_item(
    client: httpx.AsyncClient,
    *,
    title: str = "Three-seater sofa, grey",
    description: str = "Good condition, collect from Kamakwa",
    category: str = "furniture",
    condition: str = "good",
    pricing: str = "negotiable",
    price_kes: int | None = 8500,
    expect: int = 201,
    **extra: Any,
) -> dict[str, Any]:
    response = await client.post(
        "/v1/listings",
        json={
            "title": title,
            "description": description,
            "category": category,
            "condition": condition,
            "pricing": pricing,
            "price_kes": price_kes,
            **extra,
        },
    )
    assert response.status_code == expect, response.text
    return dict(response.json())


async def browse(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/v1/listings", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# The acceptance scenario
# ---------------------------------------------------------------------------
async def test_list_find_chat_and_sell(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller = await person(db_session)
    buyer = await person(db_session, at=offset_km(NYERI_TOWN, north=2.0))
    buyer_id = buyer.id

    # 1. The seller lists an item.
    listing = await list_item(await authenticate(seller))
    assert listing["availability"] == "available"
    assert listing["reach_estimate"] is not None
    assert listing["area_label"]["label"]

    # 2. A nearby buyer finds it by searching, typing only part of a word.
    found = await browse(await authenticate(buyer), q="sof")
    assert [item["id"] for item in found["items"]] == [listing["id"]]
    assert found["items"][0]["distance_bucket"] == "1_3km"

    # 3. They chat with the seller.
    buyer_client = await authenticate(buyer)
    chat = (await buyer_client.post(f"/v1/listings/{listing['id']}/chat")).json()
    assert chat["kind"] == "listing" and chat["listing_id"] == listing["id"]
    asked = await buyer_client.post(
        f"/v1/conversations/{chat['id']}/messages", json={"body": "Is the sofa still available?"}
    )
    assert asked.status_code == 201
    seller_client = await authenticate(seller)
    inbox = (await seller_client.get("/v1/conversations?folder=chats")).json()["items"]
    assert [item["id"] for item in inbox] == [chat["id"]]
    replied = await seller_client.post(
        f"/v1/conversations/{chat['id']}/messages", json={"body": "Yes, come on Saturday"}
    )
    assert replied.status_code == 201

    # 4. The seller marks it sold, to that buyer.
    buyers = (await seller_client.get(f"/v1/listings/{listing['id']}/buyers")).json()
    assert [(item["user"]["id"], item["accepted"]) for item in buyers] == [(str(buyer_id), True)]
    sold = await seller_client.put(
        f"/v1/listings/{listing['id']}/availability",
        json={"availability": "sold", "buyer_id": str(buyer_id)},
    )
    assert sold.status_code == 200
    assert sold.json()["availability"] == "sold"

    # It leaves browse, but the link still opens.
    assert (await browse(await authenticate(buyer), q="sofa"))["items"] == []
    still = await (await authenticate(buyer)).get(f"/v1/listings/{listing['id']}")
    assert still.json()["availability"] == "sold"
    stored = await db_session.get(Listing, uuid.UUID(listing["id"]))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.sold_to_id == buyer_id


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pricing", "price", "expect"),
    [
        ("free", None, 201),
        ("free", 0, 201),
        ("free", 500, 400),
        ("fixed", None, 400),
        ("fixed", 0, 400),
        ("negotiable", 1500, 201),
        ("fixed", 1_000_000_000, 400),
    ],
)
async def test_price_rules(
    db_session: AsyncSession,
    authenticate: Authenticate,
    pricing: str,
    price: int | None,
    expect: int,
) -> None:
    client = await authenticate(await person(db_session))
    created = await list_item(
        client, title=f"Item {pricing} {price}", pricing=pricing, price_kes=price, expect=expect
    )
    if expect == 201 and pricing == "free":
        assert created["price_kes"] == 0


async def test_listing_needs_a_home_area(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await make_user(db_session, created_days_ago=30))
    await list_item(client, expect=409)


async def test_categories_come_from_configuration(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    response = await client.post(
        "/v1/listings",
        json={
            "title": "x",
            "category": "weapons",
            "condition": "good",
            "pricing": "free",
        },
    )
    assert response.status_code == 400
    assert "furniture" in response.json()["error"]["details"]["categories"]


async def test_photos_must_be_listing_uploads_and_at_most_eight(
    db_session: AsyncSession, authenticate: Authenticate, storage: MemoryStorageProvider
) -> None:
    seller = await person(db_session)
    photos = [
        await make_ready_image(db_session, storage, owner=seller, purpose=MediaPurpose.listing)
        for _ in range(9)
    ]
    wrong = await make_ready_image(db_session, storage, owner=seller, purpose=MediaPurpose.post)
    client = await authenticate(seller)

    eight = await list_item(client, media_ids=[str(photo.id) for photo in photos[:8]])
    assert len(eight["images"]) == 8
    await list_item(client, title="Nine", media_ids=[str(photo.id) for photo in photos], expect=400)
    await list_item(client, title="Wrong", media_ids=[str(wrong.id)], expect=400)


async def test_new_accounts_have_a_lower_daily_limit(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    client = await authenticate(await person(db_session, age=0))
    for index in range(settings.new_account_listings_per_day):
        await list_item(client, title=f"Item number {index}")

    await list_item(client, title="One too many", expect=429)


async def test_listing_is_idempotent_with_a_key(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    key = {"Idempotency-Key": str(uuid.uuid4())}
    body = {
        "title": "Bike",
        "category": "sports",
        "condition": "fair",
        "pricing": "fixed",
        "price_kes": 6000,
    }

    first = await client.post("/v1/listings", json=body, headers=key)
    second = await client.post("/v1/listings", json=body, headers=key)

    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replayed") == "true"


# ---------------------------------------------------------------------------
# Anti-abuse
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "description", "term"),
    [
        ("Pistol for sale", "", "pistol"),
        ("Carvings", "Genuine IVORY ornaments", "ivory"),
        ("Notes", "fake-currency, high quality", "fake currency"),
    ],
)
async def test_prohibited_items_are_refused_and_the_term_is_named(
    db_session: AsyncSession, authenticate: Authenticate, title: str, description: str, term: str
) -> None:
    client = await authenticate(await person(db_session))

    refused = await list_item(client, title=title, description=description, expect=400)

    assert refused["error"]["details"] == {"reason": "prohibited_item", "term": term}


@pytest.mark.parametrize(
    "title", ["Glue gun, hardly used", "Spray gun for painting", "Rifleman jacket"]
)
async def test_innocent_listings_are_not_caught(
    db_session: AsyncSession, authenticate: Authenticate, title: str
) -> None:
    client = await authenticate(await person(db_session))
    await list_item(client, title=title, category="household")


async def test_an_edit_cannot_slip_past_the_prohibited_list(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    listing = await list_item(client)

    response = await client.patch(
        f"/v1/listings/{listing['id']}", json={"description": "Also selling bhang"}
    )

    assert response.status_code == 400


async def test_relisting_the_same_item_is_a_duplicate(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    first = await list_item(client, title="iPhone 11, 64GB!!", description="Clean")

    again = await list_item(client, title="iphone 11 64gb", description="clean.", expect=409)

    assert again["error"]["details"] == {"reason": "duplicate", "listing_id": first["id"]}


async def test_duplicates_are_per_seller_and_only_while_live(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller = await person(db_session)
    first = await list_item(await authenticate(seller))
    # Another seller may list the same thing.
    await list_item(await authenticate(await person(db_session)))
    # Once sold, the same seller may list another one.
    await (await authenticate(seller)).put(
        f"/v1/listings/{first['id']}/availability", json={"availability": "sold"}
    )
    await list_item(await authenticate(seller))


# ---------------------------------------------------------------------------
# Browse and search
# ---------------------------------------------------------------------------
async def test_browse_shows_nearby_listings_for_sale(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    near = await list_item(await authenticate(await person(db_session)), title="Near sofa")
    await list_item(await authenticate(await person(db_session, at=NAIROBI)), title="Far sofa")
    reserved = await list_item(
        await authenticate(seller := await person(db_session)), title="Reserved bed"
    )
    await (await authenticate(seller)).put(
        f"/v1/listings/{reserved['id']}/availability", json={"availability": "reserved"}
    )
    expired = await list_item(await authenticate(await person(db_session)), title="Old table")
    await db_session.execute(
        update(Listing)
        .where(Listing.id == uuid.UUID(expired["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(days=1))
    )
    await db_session.commit()

    found = await browse(await authenticate(await person(db_session)))

    assert {item["id"] for item in found["items"]} == {near["id"], reserved["id"]}
    assert found["sort"] == "newest"
    assert 2 <= found["radius_km"] <= 30


async def test_blocked_sellers_are_left_out(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller, buyer = await person(db_session), await person(db_session)
    listing = await list_item(await authenticate(seller))
    await block_user(db_session, blocker=seller, blocked_id=buyer.id)

    client = await authenticate(buyer)
    assert (await browse(client))["items"] == []
    assert (await client.get(f"/v1/listings/{listing['id']}")).status_code == 404
    assert (await client.post(f"/v1/listings/{listing['id']}/chat")).status_code == 404


async def test_search_is_prefix_matched_and_bilingual(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    sofa = await list_item(client, title="Sofa set", description="Leather cushions")
    mtumba = await list_item(
        client,
        title="Mtumba bale",
        description="Nguo safi za watoto",
        category="clothing_and_shoes",
    )
    buyer = await authenticate(await person(db_session))

    assert [i["id"] for i in (await browse(buyer, q="cush"))["items"]] == [sofa["id"]]
    assert [i["id"] for i in (await browse(buyer, q="mtumba"))["items"]] == [mtumba["id"]]
    assert [i["id"] for i in (await browse(buyer, q="nguo watoto"))["items"]] == [mtumba["id"]]
    # Every word must match.
    assert (await browse(buyer, q="sofa watoto"))["items"] == []
    # Punctuation is not syntax.
    assert (await browse(buyer, q="sofa & | !:*"))["items"][0]["id"] == sofa["id"]


async def test_a_title_match_ranks_above_a_description_match(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    in_description = await list_item(client, title="Coffee table", description="Comes with a lamp")
    in_title = await list_item(client, title="Standing lamp", description="Warm light")

    found = await browse(await authenticate(await person(db_session)), q="lamp")

    assert [item["id"] for item in found["items"]] == [in_title["id"], in_description["id"]]
    assert found["sort"] == "relevance"


async def test_filters(db_session: AsyncSession, authenticate: Authenticate) -> None:
    client = await authenticate(await person(db_session))
    cheap = await list_item(
        client, title="Kettle", category="household", condition="new", price_kes=900
    )
    dear = await list_item(
        client, title="Fridge", category="household", condition="good", price_kes=25_000
    )
    await list_item(client, title="Shoes", category="clothing_and_shoes", price_kes=1500)
    buyer = await authenticate(await person(db_session))

    by_category = await browse(buyer, category="household")
    assert {item["id"] for item in by_category["items"]} == {cheap["id"], dear["id"]}
    assert [i["id"] for i in (await browse(buyer, condition="new"))["items"]] == [cheap["id"]]
    ranged = await browse(buyer, min_price=1000, max_price=30_000, category="household")
    assert [item["id"] for item in ranged["items"]] == [dear["id"]]
    bad = await buyer.get("/v1/listings", params={"min_price": 10, "max_price": 5})
    assert bad.status_code == 400


async def test_sorting_by_price_paginates(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    prices = [3000, 500, 12_000, 500, 7000]
    for index, price in enumerate(prices):
        await list_item(client, title=f"Thing {index}", price_kes=price)
    buyer = await authenticate(await person(db_session))

    seen: list[int] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"sort": "price_asc", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await browse(buyer, **params)
        seen.extend(item["price_kes"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break

    assert seen == sorted(prices)
    descending = await browse(buyer, sort="price_desc")
    assert [item["price_kes"] for item in descending["items"]] == sorted(prices, reverse=True)


async def test_nearest_sorts_by_band_and_paginates(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    far = await list_item(
        await authenticate(await person(db_session, at=offset_km(NYERI_TOWN, north=6))),
        title="Far chair",
    )
    near = await list_item(await authenticate(await person(db_session)), title="Near chair")
    middle = await list_item(
        await authenticate(await person(db_session, at=offset_km(NYERI_TOWN, north=2))),
        title="Middle chair",
    )
    buyer = await authenticate(await person(db_session))

    ids: list[str] = []
    bands: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"sort": "nearest", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await browse(buyer, **params)
        ids.extend(item["id"] for item in page["items"])
        bands.extend(item["distance_bucket"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break

    assert ids == [near["id"], middle["id"], far["id"]]
    assert bands == ["under_1km", "1_3km", "3_10km"]


async def test_the_distance_filter_is_capped(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    karatina = await list_item(
        await authenticate(await person(db_session, at=KARATINA)), title="Karatina bike"
    )
    buyer = await authenticate(await person(db_session))

    wide = await browse(buyer, distance_km=100)

    assert wide["radius_km"] == 50
    assert karatina["id"] in [item["id"] for item in wide["items"]]
    narrow = await browse(buyer, distance_km=1)
    assert karatina["id"] not in [item["id"] for item in narrow["items"]]


# ---------------------------------------------------------------------------
# Chat, saving, selling
# ---------------------------------------------------------------------------
async def test_a_buyer_sends_one_message_until_the_seller_replies(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller, buyer = await person(db_session), await person(db_session)
    listing = await list_item(await authenticate(seller))
    client = await authenticate(buyer)
    chat = (await client.post(f"/v1/listings/{listing['id']}/chat")).json()
    again = (await client.post(f"/v1/listings/{listing['id']}/chat")).json()
    assert chat["id"] == again["id"]

    await client.post(f"/v1/conversations/{chat['id']}/messages", json={"body": "Still there?"})
    second = await client.post(f"/v1/conversations/{chat['id']}/messages", json={"body": "Hello?"})

    assert second.status_code == 409
    seller_client = await authenticate(seller)
    assert (await seller_client.get("/v1/conversations?folder=requests")).json()["items"] == []
    note = (
        await db_session.execute(select(Notification).where(Notification.user_id == seller.id))
    ).scalar_one()
    assert note.type == "listing_inquiry"
    assert note.data["listing_id"] == listing["id"]


async def test_each_buyer_gets_their_own_conversation(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    listing = await list_item(await authenticate(await person(db_session)))
    first = (
        await (await authenticate(await person(db_session))).post(
            f"/v1/listings/{listing['id']}/chat"
        )
    ).json()
    second = (
        await (await authenticate(await person(db_session))).post(
            f"/v1/listings/{listing['id']}/chat"
        )
    ).json()
    assert first["id"] != second["id"]


async def test_you_cannot_ask_about_your_own_listing(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    client = await authenticate(await person(db_session))
    listing = await list_item(client)
    assert (await client.post(f"/v1/listings/{listing['id']}/chat")).status_code == 400


async def test_a_sold_listing_cannot_start_new_chats(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller = await person(db_session)
    listing = await list_item(await authenticate(seller))
    await (await authenticate(seller)).put(
        f"/v1/listings/{listing['id']}/availability", json={"availability": "sold"}
    )

    response = await (await authenticate(await person(db_session))).post(
        f"/v1/listings/{listing['id']}/chat"
    )

    assert response.status_code == 409


async def test_only_someone_who_asked_can_be_named_the_buyer(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    seller = await person(db_session)
    stranger = await person(db_session)
    listing = await list_item(await authenticate(seller))

    response = await (await authenticate(seller)).put(
        f"/v1/listings/{listing['id']}/availability",
        json={"availability": "sold", "buyer_id": str(stranger.id)},
    )

    assert response.status_code == 400


async def test_only_the_seller_changes_a_listing(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    listing = await list_item(await authenticate(await person(db_session)))
    other = await authenticate(await person(db_session))

    for method, path, body in (
        ("PATCH", f"/v1/listings/{listing['id']}", {"title": "Mine now"}),
        ("PUT", f"/v1/listings/{listing['id']}/availability", {"availability": "sold"}),
        ("POST", f"/v1/listings/{listing['id']}/renew", None),
        ("DELETE", f"/v1/listings/{listing['id']}", None),
        ("GET", f"/v1/listings/{listing['id']}/buyers", None),
    ):
        response = await other.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.status_code)


async def test_saving_listings(db_session: AsyncSession, authenticate: Authenticate) -> None:
    seller = await person(db_session)
    shopper = await person(db_session)
    first = await list_item(await authenticate(seller), title="Desk")
    second = await list_item(await authenticate(seller), title="Chair")
    client = await authenticate(shopper)

    for listing in (first, second, first):
        assert (await client.put(f"/v1/listings/{listing['id']}/save")).status_code == 204
    saved = (await client.get("/v1/listings/saved")).json()
    assert [item["id"] for item in saved["items"]] == [second["id"], first["id"]]
    assert all(item["saved"] for item in saved["items"])

    await client.delete(f"/v1/listings/{first['id']}/save")
    remaining = (await client.get("/v1/listings/saved")).json()
    assert [item["id"] for item in remaining["items"]] == [second["id"]]

    # A listing its seller deletes drops out of everyone's saved list.
    await (await authenticate(seller)).delete(f"/v1/listings/{second['id']}")
    after = (await (await authenticate(shopper)).get("/v1/listings/saved")).json()
    assert after["items"] == []


async def test_renewing_and_its_window(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    client = await authenticate(await person(db_session))
    listing = await list_item(client)
    await db_session.execute(
        update(Listing)
        .where(Listing.id == uuid.UUID(listing["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(days=2))
    )
    await db_session.commit()

    renewed = await client.post(f"/v1/listings/{listing['id']}/renew")
    assert renewed.status_code == 200
    assert renewed.json()["is_expired"] is False

    await db_session.execute(
        update(Listing)
        .where(Listing.id == uuid.UUID(listing["id"]))
        .values(
            expires_at=datetime.now(UTC) - timedelta(days=settings.listing_renewal_grace_days + 1)
        )
    )
    await db_session.commit()
    late = await client.post(f"/v1/listings/{listing['id']}/renew")
    assert late.status_code == 409


async def test_reports_auto_hide_a_listing(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    seller = await person(db_session)
    listing = await list_item(await authenticate(seller), title="Suspicious phone")
    for _ in range(settings.report_auto_hide_threshold):
        reporter = await person(db_session, age=60)
        response = await (await authenticate(reporter)).post(
            "/v1/reports",
            json={"target_type": "listing", "target_id": listing["id"], "reason": "scam"},
        )
        assert response.status_code == 201

    buyer = await authenticate(await person(db_session))
    assert (await buyer.get(f"/v1/listings/{listing['id']}")).status_code == 404
    assert (await browse(buyer))["items"] == []
    own = (await (await authenticate(seller)).get(f"/v1/listings/{listing['id']}")).json()
    assert own["status"] == "hidden_pending_review"


async def test_export_and_purge(
    db_session: AsyncSession, authenticate: Authenticate, settings: Any
) -> None:
    from app.modules.users.tasks import purge_deleted_accounts

    seller = await person(db_session)
    listing = await list_item(await authenticate(seller))
    other = await list_item(await authenticate(await person(db_session)), title="Lamp")
    # One client, whose token `authenticate` swaps: sign in again before each use.
    client = await authenticate(seller)
    await client.put(f"/v1/listings/{other['id']}/save")

    export = (await client.get("/v1/me/export")).json()
    assert [item["id"] for item in export["marketplace"]["listings"]] == [listing["id"]]
    assert [item["listing_id"] for item in export["marketplace"]["saved_listings"]] == [other["id"]]

    await db_session.refresh(seller)
    seller.deletion_requested_at = datetime.now(UTC) - timedelta(
        days=settings.deletion_grace_period_days + 1
    )
    await db_session.flush()
    await purge_deleted_accounts(db_session, settings=settings)

    stored = await db_session.get(Listing, uuid.UUID(listing["id"]))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.deleted_at is not None


async def test_editing_a_listing(
    db_session: AsyncSession, authenticate: Authenticate, storage: MemoryStorageProvider
) -> None:
    seller = await person(db_session)
    photo = await make_ready_image(db_session, storage, owner=seller, purpose=MediaPurpose.listing)
    client = await authenticate(seller)
    listing = await list_item(client, pricing="fixed", price_kes=9000)

    edited = await client.patch(
        f"/v1/listings/{listing['id']}",
        json={"title": "Sofa, now cheaper", "price_kes": 7000, "media_ids": [str(photo.id)]},
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["title"], body["price_kes"], body["pricing"]) == (
        "Sofa, now cheaper",
        7000,
        "fixed",
    )
    assert [image["media_id"] for image in body["images"]] == [str(photo.id)]

    free = await client.patch(f"/v1/listings/{listing['id']}", json={"pricing": "free"})
    assert (free.json()["pricing"], free.json()["price_kes"]) == ("free", 0)
    # Found by its new title, not its old one.
    buyer = await authenticate(await person(db_session))
    assert [i["id"] for i in (await browse(buyer, q="cheaper"))["items"]] == [listing["id"]]

    seller_client = await authenticate(seller)
    await seller_client.put(
        f"/v1/listings/{listing['id']}/availability", json={"availability": "sold"}
    )
    late = await seller_client.patch(f"/v1/listings/{listing['id']}", json={"title": "Changed"})
    assert late.status_code == 409
