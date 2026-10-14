"""The Phase 3 acceptance test: realtime across two API processes.

Two real `uvicorn` processes on their own ports, sharing the test database and
the test Redis, exactly as two production workers would. A client connected to
one receives what was sent through the other — which only works if the hub really
fans out through Redis rather than in process memory.

Everything here is committed for real, because the servers are separate processes
and cannot see a test's transaction. The tables are truncated afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.core.config import Settings
from app.core.db import session_scope
from app.core.providers.push import RecordingPushProvider
from app.core.realtime import online_user_ids
from app.core.redis import get_redis
from app.modules.auth import service as auth_service
from app.workers.tasks import deliver_message_push
from tests.conftest import PROJECT_ROOT
from tests.factories import NYERI_TOWN, make_user

STARTUP_TIMEOUT_SECONDS = 60.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Worker:
    port: int
    process: subprocess.Popen[bytes]

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/ws"


@pytest.fixture(scope="module")
def workers() -> Iterator[list[Worker]]:
    """Two API processes, configured as the test suite is."""
    environment = {
        **os.environ,
        # A short auth window so the unauthenticated-socket test is quick.
        "WS_AUTH_TIMEOUT_SECONDS": "1",
        "PYTHONUNBUFFERED": "1",
    }
    started: list[Worker] = []
    for _ in range(2):
        port = _free_port()
        process = subprocess.Popen(  # noqa: S603 — a fixed command, our own app
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.append(Worker(port=port, process=process))
    try:
        for worker in started:
            _wait_until_up(worker)
        yield started
    finally:
        for worker in started:
            worker.process.terminate()
        for worker in started:
            try:
                worker.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                worker.process.kill()


def _wait_until_up(worker: Worker) -> None:
    import time

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if worker.process.poll() is not None:
            raise RuntimeError(f"API worker on port {worker.port} exited during startup")
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{worker.http}/health", timeout=1.0).status_code == 200:
                return
        time.sleep(0.2)
    raise RuntimeError(f"API worker on port {worker.port} did not start")


@dataclass
class Person:
    id: uuid.UUID
    display_name: str
    access_token: str
    refresh_token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


@pytest_asyncio.fixture(loop_scope="session")
async def people(engine: AsyncEngine, settings: Settings) -> AsyncIterator[list[Person]]:
    """Three committed users with tokens; everything is truncated afterwards."""
    created: list[Person] = []
    async with session_scope() as session:
        for _ in range(3):
            user = await make_user(session, at=NYERI_TOWN)
            pair = await auth_service.issue_tokens(session, user=user, settings=settings)
            created.append(
                Person(
                    id=user.id,
                    display_name=user.display_name,
                    access_token=pair.access_token,
                    refresh_token=pair.refresh_token,
                )
            )
    try:
        yield created
    finally:
        async with engine.begin() as connection:
            tables = [
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename NOT IN ('spatial_ref_sys', 'alembic_version')"
                    )
                )
            ]
            joined = ", ".join(f'"{table}"' for table in tables)
            await connection.execute(text(f"TRUNCATE TABLE {joined} CASCADE"))


async def _socket(worker: Worker, person: Person, *, header: bool = True) -> ClientConnection:
    """A connected, authenticated socket, past its `ready` frame."""
    if header:
        connection = await connect(worker.ws, additional_headers=person.headers)
    else:
        connection = await connect(worker.ws)
        await connection.send(json.dumps({"type": "auth", "token": person.access_token}))
    ready = json.loads(await asyncio.wait_for(connection.recv(), 5))
    assert ready["type"] == "ready"
    assert ready["data"]["user_id"] == str(person.id)
    return connection


async def _next_event(connection: ClientConnection, event_type: str, **data: Any) -> dict[str, Any]:
    """The next frame of a type, optionally with matching data fields."""

    async def _read() -> dict[str, Any]:
        while True:
            frame = json.loads(await connection.recv())
            if frame["type"] == event_type and all(
                frame["data"].get(key) == value for key, value in data.items()
            ):
                return dict(frame)

    return await asyncio.wait_for(_read(), 5)


async def _send(
    client: httpx.AsyncClient, worker: Worker, person: Person, conversation_id: str, body: str
) -> httpx.Response:
    return await client.post(
        f"{worker.http}/v1/conversations/{conversation_id}/messages",
        json={"body": body},
        headers=person.headers,
    )


async def _accepted_conversation(
    client: httpx.AsyncClient, worker: Worker, first: Person, second: Person
) -> str:
    opened = await client.post(
        f"{worker.http}/v1/conversations/direct",
        json={"user_id": str(second.id)},
        headers=first.headers,
    )
    assert opened.status_code == 200, opened.text
    conversation_id = str(opened.json()["id"])
    assert (await _send(client, worker, first, conversation_id, "Habari")).status_code == 201
    assert (await _send(client, worker, second, conversation_id, "Poa")).status_code == 201
    return conversation_id


async def test_two_clients_exchange_messages_across_two_workers(
    workers: list[Worker], people: list[Person], push: RecordingPushProvider
) -> None:
    one, two = workers
    alice, bob, _carol = people
    async with httpx.AsyncClient(timeout=10) as client:
        conversation_id = await _accepted_conversation(client, one, alice, bob)

        alice_socket = await _socket(one, alice)  # header auth, worker one
        bob_socket = await _socket(two, bob, header=False)  # first-frame auth, worker two
        await asyncio.sleep(0.3)  # let both hubs finish subscribing

        # Alice sends through worker one; Bob, on worker two, is rung.
        sent = await _send(client, one, alice, conversation_id, "Niko njiani")
        assert sent.status_code == 201
        event = await _next_event(bob_socket, "message.created")
        assert event["data"]["conversation_id"] == conversation_id
        assert event["data"]["seq"] == sent.json()["seq"]

        # The doorbell carries no content; Bob fetches it, through worker two.
        fetched = await client.get(
            f"{two.http}/v1/conversations/{conversation_id}/messages",
            params={"after_seq": event["data"]["seq"] - 1},
            headers=bob.headers,
        )
        assert [item["body"] for item in fetched.json()["items"]] == ["Niko njiani"]

        # And back the other way.
        reply = await _send(client, two, bob, conversation_id, "Sawa, nakungoja")
        assert reply.status_code == 201
        # Alice's socket also heard about her own message (a sender's other
        # devices are rung too), so wait for Bob's specifically.
        back = await _next_event(alice_socket, "message.created", message_id=reply.json()["id"])
        assert back["data"]["seq"] == reply.json()["seq"]

        # Pings are answered.
        await alice_socket.send(json.dumps({"type": "ping"}))
        await _next_event(alice_socket, "pong")

        await alice_socket.close()
        await bob_socket.close()


async def test_concurrent_sends_through_both_workers_get_contiguous_sequence_numbers(
    workers: list[Worker], people: list[Person]
) -> None:
    """Numbering is serialised by the conversation row lock, whichever process
    takes the request — so sequence order is commit order."""
    one, two = workers
    alice, bob, _carol = people
    async with httpx.AsyncClient(timeout=20) as client:
        conversation_id = await _accepted_conversation(client, one, alice, bob)

        responses = await asyncio.gather(
            *(
                _send(
                    client,
                    one if index % 2 else two,
                    alice if index % 3 else bob,
                    conversation_id,
                    f"race {index}",
                )
                for index in range(12)
            )
        )

        assert [response.status_code for response in responses] == [201] * 12
        seqs = sorted(response.json()["seq"] for response in responses)
        assert seqs == list(range(3, 15))


async def test_an_offline_user_gets_a_push(
    workers: list[Worker], people: list[Person], push: RecordingPushProvider
) -> None:
    one, two = workers
    alice, bob, _carol = people
    async with httpx.AsyncClient(timeout=10) as client:
        conversation_id = await _accepted_conversation(client, one, alice, bob)
        registered = await client.post(
            f"{two.http}/v1/notifications/devices",
            json={"token": "bobs-phone", "platform": "android"},
            headers=bob.headers,
        )
        assert registered.status_code == 204

        # Online while connected, offline once the socket closes.
        bob_socket = await _socket(two, bob)
        assert await online_user_ids(get_redis(), [bob.id]) == {bob.id}
        await bob_socket.close()
        for _ in range(50):
            if not await online_user_ids(get_redis(), [bob.id]):
                break
            await asyncio.sleep(0.1)
        assert await online_user_ids(get_redis(), [bob.id]) == set()

        sent = await _send(client, one, alice, conversation_id, "Uko wapi?")
        assert sent.status_code == 201

    # The API queued the push in Redis; run it as the worker would.
    from arq.connections import RedisSettings, create_pool

    from app.core.config import get_settings

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        queued = await pool.queued_jobs()
    finally:
        await pool.aclose()
    # The setup's reply queued one too; queue order is not guaranteed.
    (job,) = [
        job
        for job in queued
        if job.function == "deliver_message_push" and job.args == (sent.json()["id"],)
    ]

    result = await deliver_message_push({}, *job.args)

    assert result == {"sent": 1, "skipped": 0}
    (message,) = push.messages_to("bobs-phone")
    assert message.title == alice.display_name
    assert message.body == "New message"


async def test_blocked_users_cannot_message_each_other(
    workers: list[Worker], people: list[Person]
) -> None:
    one, two = workers
    alice, bob, _carol = people
    async with httpx.AsyncClient(timeout=10) as client:
        conversation_id = await _accepted_conversation(client, one, alice, bob)
        alice_socket = await _socket(one, alice)

        blocked = await client.post(
            f"{two.http}/v1/me/blocks", json={"user_id": str(alice.id)}, headers=bob.headers
        )
        assert blocked.status_code == 201

        for sender, worker in ((alice, one), (bob, two)):
            response = await _send(client, worker, sender, conversation_id, "Hello?")
            assert response.status_code == 404
        reopened = await client.post(
            f"{one.http}/v1/conversations/direct",
            json={"user_id": str(bob.id)},
            headers=alice.headers,
        )
        assert reopened.status_code == 404

        # Nothing reached Alice's socket.
        with pytest.raises(TimeoutError):
            await _next_event(alice_socket, "message.created")
        await alice_socket.close()


async def test_message_requests_across_workers(workers: list[Worker], people: list[Person]) -> None:
    one, two = workers
    _alice, bob, carol = people
    async with httpx.AsyncClient(timeout=10) as client:
        opened = await client.post(
            f"{one.http}/v1/conversations/direct",
            json={"user_id": str(bob.id)},
            headers=carol.headers,
        )
        conversation_id = str(opened.json()["id"])
        bob_socket = await _socket(two, bob)
        await asyncio.sleep(0.3)

        first = await _send(client, one, carol, conversation_id, "Is the sofa still available?")
        assert first.status_code == 201
        await _next_event(bob_socket, "notification.created")
        second = await _send(client, two, carol, conversation_id, "Hello??")
        assert second.status_code == 409

        requests = await client.get(
            f"{two.http}/v1/conversations", params={"folder": "requests"}, headers=bob.headers
        )
        assert [item["id"] for item in requests.json()["items"]] == [conversation_id]
        await bob_socket.close()


async def test_signing_out_closes_the_socket_on_another_worker(
    workers: list[Worker], people: list[Person]
) -> None:
    one, two = workers
    alice, _bob, _carol = people
    alice_socket = await _socket(one, alice)
    await asyncio.sleep(0.3)

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{two.http}/v1/auth/logout", json={"refresh_token": alice.refresh_token}
        )
        assert response.status_code in (200, 204)

    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(alice_socket.recv(), 5)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 4401
    assert closed.value.rcvd.reason == "session_revoked"


async def test_a_socket_that_never_authenticates_is_closed(workers: list[Worker]) -> None:
    one, _two = workers
    connection = await connect(one.ws)

    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(connection.recv(), 5)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 4401


async def test_a_token_in_the_query_string_is_ignored(
    workers: list[Worker], people: list[Person]
) -> None:
    """URLs end up in proxy logs; a token there must not work."""
    one, _two = workers
    alice, _bob, _carol = people
    connection = await connect(f"{one.ws}?token={alice.access_token}")

    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(connection.recv(), 5)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 4401


async def test_a_bad_token_is_refused(workers: list[Worker]) -> None:
    one, _two = workers
    connection = await connect(one.ws, additional_headers={"Authorization": "Bearer nonsense"})

    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(connection.recv(), 5)
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.reason == "invalid_token"
