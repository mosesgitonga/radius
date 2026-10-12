"""Redis client.

One connection pool per process, shared by the cache, rate limiter, OTP store,
and (from Phase 3) pub/sub. Built lazily so imports stay side-effect free.

The arq worker opens its own connection through `arq.create_pool`; it needs an
`ArqRedis` and its own serialisation, so the two are kept apart deliberately.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: Redis | None = None

# What "Redis is unreachable" looks like when caught.
#
# redis-py wraps socket failures in its own `RedisError` hierarchy, but not
# always: a DNS failure or a connection reset can surface as a plain `OSError`
# from the event loop before redis-py sees it. Both belong here, because
# everything Redis is used for in this codebase — cached radii, area labels,
# rate-limit counters — degrades rather than breaks when it is unavailable, and a
# radius computation must never fail because its cache was weird.
#
# Deliberately not `Exception`: a TypeError from our own code is a bug, and
# swallowing it here would hide it behind a plausible-looking "cache
# unavailable" warning.
CACHE_ERRORS: tuple[type[BaseException], ...] = (RedisError, OSError)


def get_redis() -> Redis:
    """The shared Redis client.

    `decode_responses=True` because everything stored here is text: counters,
    hashed OTPs, cached radii, JSON pub/sub payloads.
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
            # Keep a Redis stall from becoming a request stall: a few hundred
            # milliseconds is already far outside normal for a local instance.
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            health_check_interval=30,
        )
    return _client


async def close_redis() -> None:
    """Release the pool. Called on shutdown and by test teardown."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def check_redis() -> bool:
    """Cheap readiness probe. Never raises; the caller reports the status."""
    try:
        await get_redis().ping()
    except Exception:
        logger.warning("redis_unavailable", exc_info=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Typed command wrappers
# ---------------------------------------------------------------------------
# redis-py shares one set of type stubs between its synchronous and asynchronous
# clients, so several commands are annotated as returning `Awaitable[T] | T`.
# Awaiting them is correct on the async client but does not type-check. These
# wrappers put the cast in one reviewed place rather than scattering per-call
# ignores through the service layer.
async def hash_get_all(client: Redis, key: str) -> dict[str, str]:
    return cast("dict[str, str]", await cast("Awaitable[Any]", client.hgetall(key)))


async def hash_increment(client: Redis, key: str, field: str, amount: int = 1) -> int:
    return cast("int", await cast("Awaitable[Any]", client.hincrby(key, field, amount)))


async def key_ttl(client: Redis, key: str) -> int:
    """Remaining lifetime in seconds. Negative when absent or without a TTL."""
    return cast("int", await cast("Awaitable[Any]", client.ttl(key)))
