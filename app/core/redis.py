"""Redis client.

One connection pool per process, shared by the cache, rate limiter, OTP store,
and (from Phase 3) pub/sub. Built lazily so imports stay side-effect free.

The arq worker opens its own connection through `arq.create_pool`; it needs an
`ArqRedis` and its own serialisation, so the two are kept apart deliberately.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: Redis | None = None


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
