"""Realtime fan-out over Redis pub/sub, and presence.

**Why a hub per process.** A message sent through one API worker must reach a
socket held by another. Every event is published to a Redis channel per
recipient user; each API process subscribes, over one Redis connection, to the
channels of exactly the users connected to it, and hands events to their sockets.
That works for any number of workers and holds no state Redis does not already
have — a crashed worker's users simply reconnect elsewhere.

**Pub/sub is a doorbell, not a mailbox.** Redis drops a message published while
nobody is subscribed, and a socket that drops mid-burst misses what was sent in
between. That is fine, because the database is the record: messages are persisted
before they are announced, and a reconnecting client asks for everything after the
last sequence number it holds. So nothing here retries, and a slow socket is
disconnected rather than buffered without bound — reconnecting is cheaper than
holding memory for a phone in a tunnel.

**Presence** is a sorted set per user of connection IDs scored by expiry, refreshed
by heartbeat. One entry per connection, so a phone and a tablet are tracked
separately and closing one does not mark the user offline. An entry whose process
died is simply never refreshed and stops counting once its score passes, so a
crashed worker cannot leave anyone "online" for ever — and therefore unpushed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Iterable
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import after_commit
from app.core.redis import CACHE_ERRORS, get_redis

logger = logging.getLogger(__name__)

USER_CHANNEL_PREFIX = "rt:user:"
CONTROL_CHANNEL = "rt:hub"
"""Always subscribed, so the reader never runs on a pub/sub connection with
nothing to listen to (which redis-py treats as an error)."""

PRESENCE_PREFIX = "presence:"

SESSION_REVOKED = "session.revoked"
"""Internal: tells sockets for these device sessions to close at once. Handled
by the socket, never forwarded to the client."""


def user_channel(user_id: uuid.UUID | str) -> str:
    return f"{USER_CHANNEL_PREFIX}{user_id}"


def _encode(event_type: str, data: dict[str, Any]) -> str:
    return json.dumps({"type": event_type, "data": data}, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
async def publish(user_ids: Iterable[uuid.UUID], event_type: str, data: dict[str, Any]) -> int:
    """Announce an event to users, wherever they are connected. Best-effort.

    Returns how many users it was published to. Never raises: by the time this
    runs the change is committed, and a recipient who misses the doorbell gets
    the change on their next sync.
    """
    recipients = sorted({str(user_id) for user_id in user_ids})
    if not recipients:
        return 0
    payload = _encode(event_type, data)
    try:
        async with get_redis().pipeline(transaction=False) as pipe:
            for user_id in recipients:
                pipe.publish(user_channel(user_id), payload)
            await pipe.execute()
    except CACHE_ERRORS:
        logger.warning("realtime_publish_failed", extra={"event_type": event_type})
        return 0
    return len(recipients)


def publish_after_commit(
    session: AsyncSession,
    user_ids: Iterable[uuid.UUID],
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Publish once the current transaction commits; never if it rolls back.

    The recipient list is captured now, while the caller still knows it.
    """
    recipients = list(user_ids)

    async def _publish() -> None:
        await publish(recipients, event_type, data)

    after_commit(session, _publish)


# ---------------------------------------------------------------------------
# The per-process hub
# ---------------------------------------------------------------------------
class Connection(Protocol):
    """What the hub needs from a socket: an ID, an owner, and a mailbox."""

    connection_id: str
    user_id: uuid.UUID

    def deliver(self, event: dict[str, Any]) -> None:
        """Queue an event without blocking. A full queue is the connection's
        problem to act on (by closing), never the hub's (by waiting)."""
        ...


class Hub:
    """Routes events from Redis to the sockets connected to this process."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url
        self._connections: dict[str, set[Connection]] = {}
        self._redis: Redis | None = None
        self._pubsub: PubSub | None = None
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def connected_users(self) -> int:
        return len(self._connections)

    async def _ensure_started(self) -> PubSub:
        if self._pubsub is None:
            # A client of its own: a subscribed connection blocks on reads, which
            # the shared pool's two-second socket timeout would keep breaking.
            self._redis = Redis.from_url(
                self._redis_url or get_settings().redis_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                health_check_interval=30,
            )
            self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            await self._pubsub.subscribe(CONTROL_CHANNEL)
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._read_loop(), name="realtime-hub")
        return self._pubsub

    async def register(self, connection: Connection) -> None:
        key = str(connection.user_id)
        async with self._lock:
            pubsub = await self._ensure_started()
            first = key not in self._connections
            self._connections.setdefault(key, set()).add(connection)
            if first:
                await pubsub.subscribe(user_channel(key))

    async def unregister(self, connection: Connection) -> None:
        key = str(connection.user_id)
        async with self._lock:
            sockets = self._connections.get(key)
            if not sockets:
                return
            sockets.discard(connection)
            if sockets:
                return
            del self._connections[key]
            if self._pubsub is not None:
                with contextlib.suppress(*CACHE_ERRORS):
                    await self._pubsub.unsubscribe(user_channel(key))

    def _dispatch(self, channel: str, raw: str) -> None:
        if not channel.startswith(USER_CHANNEL_PREFIX):
            return
        sockets = self._connections.get(channel[len(USER_CHANNEL_PREFIX) :])
        if not sockets:
            return
        try:
            event = json.loads(raw)
        except ValueError:
            logger.error("realtime_unparsable_event", extra={"channel_kind": "user"})
            return
        for connection in list(sockets):
            connection.deliver(event)

    async def _read_loop(self) -> None:
        while True:
            pubsub = self._pubsub
            if pubsub is None:
                return
            try:
                message = await pubsub.get_message(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except CACHE_ERRORS:
                # Redis restarted or the network blinked. Resubscribe everyone
                # this process holds; their clients sync to cover the gap.
                logger.warning("realtime_hub_reconnecting")
                await asyncio.sleep(1.0)
                await self._resubscribe()
                continue
            if message and message.get("type") == "message":
                self._dispatch(str(message["channel"]), str(message["data"]))

    async def _resubscribe(self) -> None:
        async with self._lock:
            if self._pubsub is None:
                return
            with contextlib.suppress(*CACHE_ERRORS):
                channels = [CONTROL_CHANNEL, *(user_channel(key) for key in self._connections)]
                await self._pubsub.subscribe(*channels)

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, *CACHE_ERRORS):
                await self._reader
        self._reader = None
        if self._pubsub is not None:
            with contextlib.suppress(*CACHE_ERRORS):
                await self._pubsub.aclose()  # type: ignore[no-untyped-call]  # redis-py stub gap
        self._pubsub = None
        if self._redis is not None:
            with contextlib.suppress(*CACHE_ERRORS):
                await self._redis.aclose()
        self._redis = None
        self._connections.clear()


_hub: Hub | None = None


def get_hub() -> Hub:
    global _hub
    if _hub is None:
        _hub = Hub()
    return _hub


async def close_hub() -> None:
    global _hub
    if _hub is not None:
        await _hub.close()
    _hub = None


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------
def _presence_key(user_id: uuid.UUID | str) -> str:
    return f"{PRESENCE_PREFIX}{user_id}"


async def mark_online(
    redis: Redis, user_id: uuid.UUID, connection_id: str, *, ttl_seconds: int
) -> None:
    """Record or refresh one connection. Called on connect and on each heartbeat."""
    now = time.time()
    key = _presence_key(user_id)
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.zremrangebyscore(key, "-inf", now)
            pipe.zadd(key, {connection_id: now + ttl_seconds})
            pipe.expire(key, ttl_seconds * 2)
            await pipe.execute()
    except CACHE_ERRORS:
        logger.warning("presence_update_failed")


async def mark_offline(redis: Redis, user_id: uuid.UUID, connection_id: str) -> None:
    with contextlib.suppress(*CACHE_ERRORS):
        await redis.zrem(_presence_key(user_id), connection_id)


async def online_user_ids(redis: Redis, user_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these users has at least one live connection.

    Fails towards *offline*: if Redis cannot answer, everyone is treated as away
    and gets a push. An extra push is a nuisance; a missed message is not.
    """
    candidates = list(dict.fromkeys(user_ids))
    if not candidates:
        return set()
    now = time.time()
    try:
        async with redis.pipeline(transaction=False) as pipe:
            for user_id in candidates:
                pipe.zcount(_presence_key(user_id), f"({now}", "+inf")
            counts = await pipe.execute()
    except CACHE_ERRORS:
        logger.warning("presence_lookup_failed")
        return set()
    return {user_id for user_id, count in zip(candidates, counts, strict=True) if count}
