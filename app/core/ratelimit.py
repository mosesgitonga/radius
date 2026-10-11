"""Redis-backed rate limiting.

Counting is done in a Lua script so the increment and the expiry are one atomic
round trip. A read-then-write from Python would let concurrent requests race past
the limit, which is exactly what matters for the endpoints this protects: OTP
requests, posting, and messaging.

The window is a fixed window anchored at its first request, not a calendar
window. "5 OTP requests per hour" therefore means five from the first attempt
onwards, with no cliff at the top of the hour for a caller to queue against.
That also makes cooldowns ("one resend per 60 seconds") and daily caps the same
mechanism with different numbers.

Rejected requests still increment the counter. Someone hammering a limit does not
get a cheaper window by hammering it harder, and the expiry is set only on the
first request so the window cannot be extended indefinitely.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings, get_settings
from app.core.errors import RateLimitedError
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

Scope = Literal["ip", "user", "user_or_ip", "global"]

KEY_PREFIX = "rl"

# Returns {count_after_this_request, milliseconds_until_the_window_resets}.
_HIT_SCRIPT = """
local cost = tonumber(ARGV[2])
local current = redis.call('INCRBY', KEYS[1], cost)
local ttl = redis.call('PTTL', KEYS[1])
if current == cost or ttl < 0 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {current, ttl}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


class RateLimiter:
    """Counts requests per bucket and identity."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        self._script = redis.register_script(_HIT_SCRIPT)

    @staticmethod
    def build_key(bucket: str, identity: str) -> str:
        return f"{KEY_PREFIX}:{bucket}:{identity}"

    async def hit(
        self,
        bucket: str,
        identity: str,
        *,
        limit: int,
        window_seconds: int,
        cost: int = 1,
        fail_open: bool | None = None,
    ) -> RateLimitResult:
        """Record a request and report whether it is within the limit.

        `fail_open` overrides the configured default for callers where abuse costs
        more than downtime — OTP issuing and payments should fail closed.
        """
        if not self._settings.rate_limit_enabled:
            return RateLimitResult(True, limit, limit, 0)

        key = self.build_key(bucket, identity)
        try:
            raw: Any = await self._script(keys=[key], args=[window_seconds * 1000, cost])
            count = int(raw[0])
            ttl_ms = int(raw[1])
        except RedisError:
            open_on_failure = (
                self._settings.rate_limit_fail_open if fail_open is None else fail_open
            )
            logger.error(
                "rate_limit_backend_unavailable",
                extra={"bucket": bucket, "fail_open": open_on_failure},
                exc_info=True,
            )
            if open_on_failure:
                return RateLimitResult(True, limit, limit, 0)
            return RateLimitResult(False, limit, 0, window_seconds)

        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            # Round up: reporting 0 seconds would invite an immediate retry that
            # is still inside the window.
            retry_after_seconds=max(1, math.ceil(ttl_ms / 1000)),
        )

    async def peek(self, bucket: str, identity: str, *, limit: int) -> int:
        """Requests remaining in the current window, without consuming one."""
        try:
            raw = await self._redis.get(self.build_key(bucket, identity))
        except RedisError:
            return limit
        return max(0, limit - int(raw or 0))

    async def reset(self, bucket: str, identity: str) -> None:
        """Clear a bucket. Used after a successful verification, and by tests."""
        try:
            await self._redis.delete(self.build_key(bucket, identity))
        except RedisError:
            logger.warning("rate_limit_reset_failed", extra={"bucket": bucket})


def get_rate_limiter() -> RateLimiter:
    """FastAPI dependency. Cheap to build: the pool and script are shared."""
    return RateLimiter(get_redis(), get_settings())


def resolve_identity(request: Request, scope: Scope) -> str:
    """Pick the identity a limit is counted against.

    `user_or_ip` is the sensible default for authenticated endpoints: it counts
    per account when there is one, and falls back to the IP so an unauthenticated
    caller cannot dodge the limit entirely. Note that many Kenyan users share a
    carrier NAT, so IP-only limits must stay generous enough not to punish a whole
    neighbourhood for one bad actor.
    """
    user_id = getattr(request.state, "user_id", None)
    client_ip = getattr(request.state, "client_ip", None) or "unknown"

    match scope:
        case "user":
            if user_id is None:
                raise RuntimeError("user-scoped rate limit used on an unauthenticated route")
            return f"u:{user_id}"
        case "ip":
            return f"ip:{client_ip}"
        case "user_or_ip":
            return f"u:{user_id}" if user_id is not None else f"ip:{client_ip}"
        case "global":
            return "all"


def rate_limit(
    *,
    bucket: str,
    limit: int,
    window_seconds: int,
    scope: Scope = "user_or_ip",
    cost: int = 1,
    fail_open: bool | None = None,
) -> Callable[[Request, Response], Awaitable[None]]:
    """Build a dependency that enforces one limit.

        @router.post("/posts", dependencies=[Depends(rate_limit(
            bucket="create_post", limit=10, window_seconds=3600, scope="user"))])

    Several can be stacked on one route — a burst limit and a daily cap, say —
    since each counts its own bucket.
    """

    async def dependency(request: Request, response: Response) -> None:
        limiter = get_rate_limiter()
        identity = resolve_identity(request, scope)
        result = await limiter.hit(
            bucket,
            identity,
            limit=limit,
            window_seconds=window_seconds,
            cost=cost,
            fail_open=fail_open,
        )
        if not result.allowed:
            logger.info("rate_limited", extra={"bucket": bucket, "scope": scope})
            raise RateLimitedError(retry_after_seconds=result.retry_after_seconds)
        # Set on the response so clients can back off before they are refused.
        response.headers.update(result.headers)

    return dependency
