"""Object-level authorization, every route in one place.

An outsider — signed in, a neighbour, but not a party — tries every route on four
private things: a direct conversation, a private group, a private event, and the
messages in that conversation. Every attempt must be refused, and no response may
carry the secret text inside them. A second test reads public content and checks
no response, anywhere, carries a coordinate, a cell or an exact distance.

`test_every_route_on_a_private_thing_is_covered` fails when a route is added on
one of these resources without being added here, so the matrix cannot quietly
fall behind the API (OWASP API1:2023, broken object level authorization).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import NYERI_TOWN, make_post, make_user
from tests.test_events import SECRET_VENUE, organise

Authenticate = Callable[[Any], Awaitable[httpx.AsyncClient]]

SECRET_MESSAGE = "the spare key is under the third flowerpot"
SECRET_GROUP_POST = "committee minutes: the treasurer resigned"
SECRET_GROUP_NAME = "Kamakwa plot owners (private)"

DENIED = {403, 404}

# Each route on a private thing, as the outsider calls it, with its body (or, for a
# GET, its query). `{x}` placeholders are filled from the World below.
MATRIX: list[tuple[str, str, dict[str, Any] | None]] = [
    # A direct conversation the outsider is not in.
    ("GET", "/v1/conversations/{conversation_id}", None),
    ("PATCH", "/v1/conversations/{conversation_id}", {"title": "Mine now"}),
    ("POST", "/v1/conversations/{conversation_id}/block-and-report", {"reason": "spam"}),
    ("POST", "/v1/conversations/{conversation_id}/invitation", {"action": "accept"}),
    ("POST", "/v1/conversations/{conversation_id}/leave", None),
    ("GET", "/v1/conversations/{conversation_id}/members", None),
    ("POST", "/v1/conversations/{conversation_id}/members", {"user_ids": ["{outsider_id}"]}),
    ("DELETE", "/v1/conversations/{conversation_id}/members/{user_id}", None),
    ("PUT", "/v1/conversations/{conversation_id}/members/{user_id}/role", {"role": "member"}),
    ("GET", "/v1/conversations/{conversation_id}/messages", None),
    ("POST", "/v1/conversations/{conversation_id}/messages", {"body": "hello"}),
    (
        "GET",
        "/v1/conversations/{conversation_id}/messages/changes",
        {"since": "2026-01-01T00:00:00Z"},
    ),
    ("PUT", "/v1/conversations/{conversation_id}/mute", {"muted": True}),
    ("POST", "/v1/conversations/{conversation_id}/read", {"seq": 1}),
    ("POST", "/v1/conversations/{conversation_id}/request", {"action": "accept"}),
    # A message in it.
    ("DELETE", "/v1/messages/{message_id}", None),
    # A private group the outsider is not in.
    ("GET", "/v1/groups/{group_id}", None),
    ("PATCH", "/v1/groups/{group_id}", {"description": "Taken over"}),
    ("DELETE", "/v1/groups/{group_id}", None),
    ("PUT", "/v1/groups/{group_id}/bans/{user_id}", {}),
    ("DELETE", "/v1/groups/{group_id}/bans/{user_id}", None),
    ("POST", "/v1/groups/{group_id}/chat", None),
    ("GET", "/v1/groups/{group_id}/events", None),
    ("POST", "/v1/groups/{group_id}/invitation", {"action": "accept"}),
    ("POST", "/v1/groups/{group_id}/invite-links", {}),
    ("GET", "/v1/groups/{group_id}/invite-links", None),
    ("DELETE", "/v1/groups/{group_id}/invite-links/{link_id}", None),
    ("POST", "/v1/groups/{group_id}/invites", {"user_ids": ["{outsider_id}"]}),
    ("POST", "/v1/groups/{group_id}/join", None),
    ("POST", "/v1/groups/{group_id}/leave", None),
    ("GET", "/v1/groups/{group_id}/members", None),
    ("DELETE", "/v1/groups/{group_id}/members/{user_id}", None),
    ("PUT", "/v1/groups/{group_id}/members/{user_id}/mute", {"until": None}),
    ("POST", "/v1/groups/{group_id}/members/{user_id}/review", {"action": "approve"}),
    ("PUT", "/v1/groups/{group_id}/members/{user_id}/role", {"role": "admin"}),
    ("POST", "/v1/groups/{group_id}/posts", {"body": "Hello", "category": "general"}),
    ("GET", "/v1/groups/{group_id}/posts", None),
    ("GET", "/v1/groups/{group_id}/posts/pending", None),
    ("DELETE", "/v1/groups/{group_id}/posts/{post_id}", None),
    ("PUT", "/v1/groups/{group_id}/posts/{post_id}/pin", {"pinned": True}),
    ("POST", "/v1/groups/{group_id}/posts/{post_id}/review", {"action": "approve"}),
    # Its post, by its own ID.
    ("GET", "/v1/posts/{post_id}", None),
    ("GET", "/v1/posts/{post_id}/comments", None),
    ("POST", "/v1/posts/{post_id}/comments", {"body": "Hi"}),
    ("PUT", "/v1/posts/{post_id}/reaction", {"kind": "like"}),
    # A private event the outsider was not invited to.
    ("GET", "/v1/events/{event_id}", None),
    ("PATCH", "/v1/events/{event_id}", {"title": "Mine now"}),
    ("DELETE", "/v1/events/{event_id}", None),
    ("GET", "/v1/events/{event_id}/attendees", None),
    ("POST", "/v1/events/{event_id}/attendees/{user_id}/review", {"action": "approve"}),
    ("POST", "/v1/events/{event_id}/cancel", {}),
    ("POST", "/v1/events/{event_id}/invites", {"user_ids": ["{outsider_id}"]}),
    ("POST", "/v1/events/{event_id}/link", None),
    ("DELETE", "/v1/events/{event_id}/link", None),
    ("PUT", "/v1/events/{event_id}/rsvp", {"response": "going"}),
]

# Routes on those resources that are not in the matrix, and why.
EXEMPT: dict[tuple[str, str], str] = {}

PRIVATE_PARAMS = ("{conversation_id}", "{group_id}", "{event_id}", "{message_id}")


@dataclass
class World:
    ids: dict[str, str]
    secrets: tuple[str, ...]


@pytest.fixture
async def world(db_session: AsyncSession, redis_client: Redis, authenticate: Authenticate) -> World:
    alice = await make_user(db_session, at=NYERI_TOWN, created_days_ago=60)
    bob = await make_user(db_session, at=NYERI_TOWN, created_days_ago=60)
    outsider = await make_user(db_session, at=NYERI_TOWN, created_days_ago=60)
    ids = {"alice": str(alice.id), "member_id": str(bob.id), "outsider_id": str(outsider.id)}

    as_alice = await authenticate(alice)
    opened = await as_alice.post("/v1/conversations/direct", json={"user_id": ids["member_id"]})
    ids["conversation_id"] = opened.json()["id"]
    sent = await (await authenticate(alice)).post(
        f"/v1/conversations/{ids['conversation_id']}/messages", json={"body": SECRET_MESSAGE}
    )
    ids["message_id"] = sent.json()["id"]
    await (await authenticate(bob)).post(
        f"/v1/conversations/{ids['conversation_id']}/messages", json={"body": "Thanks!"}
    )

    group = await (await authenticate(alice)).post(
        "/v1/groups",
        json={"name": SECRET_GROUP_NAME, "category": "estate", "visibility": "private"},
    )
    assert group.status_code == 201, group.text
    ids["group_id"] = group.json()["id"]
    posted = await (await authenticate(alice)).post(
        f"/v1/groups/{ids['group_id']}/posts",
        json={"body": SECRET_GROUP_POST, "category": "general"},
    )
    assert posted.status_code == 201, posted.text
    ids["post_id"] = posted.json().get("post", posted.json())["id"]

    event = await organise(
        await authenticate(alice),
        title="Private birthday",
        event_type="party",
        visibility="private",
        venue_name=SECRET_VENUE,
    )
    ids["event_id"] = event["id"]
    ids["link_id"] = "00000000-0000-4000-8000-000000000000"
    ids["user_id"] = ids["member_id"]

    await authenticate(outsider)
    return World(
        ids=ids, secrets=(SECRET_MESSAGE, SECRET_GROUP_POST, SECRET_GROUP_NAME, SECRET_VENUE)
    )


def _fill(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: ids[m.group(1)], value)
    if isinstance(value, dict):
        return {key: _fill(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, ids) for item in value]
    return value


async def test_an_outsider_is_refused_everywhere(client: httpx.AsyncClient, world: World) -> None:
    allowed: list[str] = []
    leaked: list[str] = []
    for method, template, body in MATRIX:
        path = _fill(template, world.ids)
        filled = _fill(body, world.ids)
        if method == "GET":
            response = await client.get(path, params=filled)
        else:
            response = await client.request(method, path, json=filled)
        if response.status_code not in DENIED:
            allowed.append(f"{method} {template} -> {response.status_code} {response.text[:200]}")
        if any(secret in response.text for secret in world.secrets):
            leaked.append(f"{method} {template}")
    assert allowed == []
    assert leaked == []


async def test_the_private_things_are_absent_from_every_list(
    client: httpx.AsyncClient, world: World
) -> None:
    for path in (
        "/v1/conversations",
        "/v1/groups/discover",
        "/v1/groups/discover?q=Kamakwa",
        "/v1/groups/mine",
        "/v1/events",
        "/v1/feed",
        "/v1/notifications",
        "/v1/sync",
    ):
        response = await client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        assert not any(secret in response.text for secret in world.secrets), path


def test_every_route_on_a_private_thing_is_covered(app: FastAPI) -> None:
    covered = {(method, template) for method, template, _ in MATRIX}
    missing = []
    for path, operations in app.openapi()["paths"].items():
        if path.startswith("/v1/admin") or not any(p in path for p in PRIVATE_PARAMS):
            continue
        for method in operations:
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            key = (method.upper(), path)
            if key not in covered and key not in EXEMPT:
                missing.append(f"{key[0]} {key[1]}")
    assert missing == [], f"Add these to MATRIX, or to EXEMPT with a reason: {missing}"


# ---------------------------------------------------------------------------
# No coordinates, cells or exact distances, in anything
# ---------------------------------------------------------------------------
FORBIDDEN_KEYS = re.compile(
    r"(^|_)(lat|lng|lon|latitude|longitude|centroid|cell|h3|geom|geometry|coordinates)($|_)"
    r"|distance_(m|km|meters|metres)$"
)


def _forbidden_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and FORBIDDEN_KEYS.search(key.lower()):
                found.append(f"{path}.{key}")
            found += _forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _forbidden_keys(item, f"{path}[{index}]")
    return found


async def test_no_response_carries_a_location(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    redis_client: Redis,
    authenticate: Authenticate,
) -> None:
    author = await make_user(db_session, at=NYERI_TOWN, created_days_ago=60)
    viewer = await make_user(db_session, at=NYERI_TOWN, created_days_ago=60)
    post = await make_post(db_session, redis_client, author=author)
    post_id, author_id = post.id, author.id
    listing = await (await authenticate(author)).post(
        "/v1/listings",
        json={
            "title": "Bicycle, good tyres",
            "category": "sports_and_outdoors",
            "condition": "good",
            "pricing": "fixed",
            "price_kes": 4500,
        },
    )
    event = await organise(await authenticate(author))
    await authenticate(viewer)
    paths = [
        "/v1/me",
        f"/v1/users/{author_id}",
        "/v1/feed",
        f"/v1/posts/{post_id}",
        "/v1/listings",
        "/v1/events",
        f"/v1/events/{event['id']}",
        "/v1/services",
        "/v1/groups/discover",
        "/v1/me/radius",
        "/v1/areas",
        "/v1/me/export",
    ]
    if listing.status_code == 201:
        paths.append(f"/v1/listings/{listing.json()['id']}")
    offenders: dict[str, list[str]] = {}
    unchecked: list[str] = []
    for path in paths:
        response = await client.get(path)
        if response.status_code != 200:
            unchecked.append(f"{path} -> {response.status_code}")
            continue
        found = _forbidden_keys(response.json())
        if found:
            offenders[path] = found
    assert unchecked == []
    assert offenders == {}
