"""Rate limiting, against a real Redis.

Rate limits are a safety feature here, not a capacity one: they are what keeps
OTP SMS costs bounded, spam out of the feed, and message floods off people's
phones. So these tests cover the abuse cases, not just the happy path.
"""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.ratelimit import RateLimiter, resolve_identity


@pytest.fixture
def limiter(redis_client: Redis, settings: Settings) -> RateLimiter:
    return RateLimiter(redis_client, settings)


async def test_requests_within_the_limit_are_allowed(limiter: RateLimiter) -> None:
    results = [
        await limiter.hit("send_otp", "ip:1.2.3.4", limit=3, window_seconds=60) for _ in range(3)
    ]

    assert [r.allowed for r in results] == [True, True, True]
    assert [r.remaining for r in results] == [2, 1, 0]


async def test_the_request_over_the_limit_is_refused(limiter: RateLimiter) -> None:
    for _ in range(3):
        await limiter.hit("send_otp", "ip:1.2.3.4", limit=3, window_seconds=60)

    result = await limiter.hit("send_otp", "ip:1.2.3.4", limit=3, window_seconds=60)

    assert not result.allowed
    assert result.remaining == 0
    assert result.retry_after_seconds > 0


async def test_retry_after_is_never_zero(limiter: RateLimiter) -> None:
    """A `Retry-After: 0` invites an immediate retry that is still refused, which
    is how a polite client turns into a hot loop."""
    await limiter.hit("burst", "ip:1.2.3.4", limit=1, window_seconds=1)

    result = await limiter.hit("burst", "ip:1.2.3.4", limit=1, window_seconds=1)

    assert result.retry_after_seconds >= 1


async def test_buckets_are_independent(limiter: RateLimiter) -> None:
    """Exhausting one limit must not lock a user out of unrelated features."""
    for _ in range(3):
        await limiter.hit("send_otp", "u:alice", limit=3, window_seconds=60)

    other = await limiter.hit("create_post", "u:alice", limit=3, window_seconds=60)

    assert other.allowed


async def test_identities_are_independent(limiter: RateLimiter) -> None:
    for _ in range(3):
        await limiter.hit("send_otp", "u:alice", limit=3, window_seconds=60)

    other = await limiter.hit("send_otp", "u:bob", limit=3, window_seconds=60)

    assert other.allowed


async def test_window_expires(limiter: RateLimiter) -> None:
    await limiter.hit("burst", "u:alice", limit=1, window_seconds=1)
    refused = await limiter.hit("burst", "u:alice", limit=1, window_seconds=1)

    await asyncio.sleep(1.1)
    allowed_again = await limiter.hit("burst", "u:alice", limit=1, window_seconds=1)

    assert not refused.allowed
    assert allowed_again.allowed


async def test_hammering_does_not_extend_the_window(
    limiter: RateLimiter, redis_client: Redis
) -> None:
    """The expiry is set on the first request only.

    If each rejected request refreshed the TTL, an attacker holding a limit
    saturated would lock the legitimate owner of that identity out indefinitely.
    """
    await limiter.hit("burst", "u:alice", limit=1, window_seconds=10)
    ttl_after_first = await redis_client.pttl(RateLimiter.build_key("burst", "u:alice"))

    for _ in range(5):
        await limiter.hit("burst", "u:alice", limit=1, window_seconds=10)
    ttl_after_hammering = await redis_client.pttl(RateLimiter.build_key("burst", "u:alice"))

    assert ttl_after_hammering <= ttl_after_first


async def test_concurrent_requests_cannot_exceed_the_limit(
    limiter: RateLimiter,
) -> None:
    """The whole reason counting happens in a Lua script.

    A read-then-write from Python would let simultaneous requests all see the
    same count and pass — exactly the pattern that sends five OTP messages when
    a flaky mobile connection retries.
    """
    results = await asyncio.gather(
        *[limiter.hit("send_otp", "u:alice", limit=5, window_seconds=60) for _ in range(25)]
    )

    assert sum(1 for result in results if result.allowed) == 5


async def test_cost_consumes_several_slots(limiter: RateLimiter) -> None:
    """A heavy operation can weigh more than one request."""
    first = await limiter.hit("upload", "u:alice", limit=10, window_seconds=60, cost=7)
    second = await limiter.hit("upload", "u:alice", limit=10, window_seconds=60, cost=7)

    assert first.allowed
    assert first.remaining == 3
    assert not second.allowed


async def test_reset_clears_a_bucket(limiter: RateLimiter) -> None:
    """Used after a successful verification, so a user who got in is not still
    counted against the attempt limit."""
    for _ in range(3):
        await limiter.hit("verify_otp", "u:alice", limit=3, window_seconds=600)

    await limiter.reset("verify_otp", "u:alice")

    assert (await limiter.hit("verify_otp", "u:alice", limit=3, window_seconds=600)).allowed


async def test_peek_does_not_consume(limiter: RateLimiter) -> None:
    await limiter.hit("send_otp", "u:alice", limit=5, window_seconds=60)

    remaining = await limiter.peek("send_otp", "u:alice", limit=5)
    remaining_again = await limiter.peek("send_otp", "u:alice", limit=5)

    assert remaining == remaining_again == 4


async def test_peek_on_an_untouched_bucket_reports_the_full_limit(
    limiter: RateLimiter,
) -> None:
    assert await limiter.peek("send_otp", "u:nobody", limit=5) == 5


# ---------------------------------------------------------------------------
# Redis failure
# ---------------------------------------------------------------------------
class _BrokenRedis:
    """Stands in for an unreachable Redis."""

    def register_script(self, script: str) -> object:
        async def fail(*args: object, **kwargs: object) -> object:
            raise RedisError("connection refused")

        return fail

    async def get(self, key: str) -> object:
        raise RedisError("connection refused")

    async def delete(self, key: str) -> object:
        raise RedisError("connection refused")


async def test_fail_open_allows_traffic_when_redis_is_down(settings: Settings) -> None:
    """The default: a Redis blip must not take the whole API down."""
    limiter = RateLimiter(_BrokenRedis(), settings)  # type: ignore[arg-type]

    result = await limiter.hit("create_post", "u:alice", limit=5, window_seconds=60)

    assert result.allowed


async def test_fail_closed_refuses_traffic_when_redis_is_down(
    settings: Settings,
) -> None:
    """Endpoints where abuse costs more than downtime opt out of failing open.

    An OTP endpoint with no working limiter sends real SMS at someone else's
    expense, so refusing is the cheaper failure.
    """
    limiter = RateLimiter(_BrokenRedis(), settings)  # type: ignore[arg-type]

    result = await limiter.hit("send_otp", "u:alice", limit=5, window_seconds=60, fail_open=False)

    assert not result.allowed
    assert result.retry_after_seconds == 60


async def test_disabling_rate_limits_bypasses_redis_entirely(
    redis_client: Redis, settings: Settings
) -> None:
    disabled = settings.model_copy(update={"rate_limit_enabled": False})
    limiter = RateLimiter(redis_client, disabled)

    results = [
        await limiter.hit("send_otp", "u:alice", limit=1, window_seconds=60) for _ in range(5)
    ]

    assert all(result.allowed for result in results)


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------
class _FakeState:
    def __init__(self, user_id: str | None, client_ip: str | None) -> None:
        self.user_id = user_id
        self.client_ip = client_ip


class _FakeRequest:
    def __init__(self, user_id: str | None = None, client_ip: str | None = None) -> None:
        self.state = _FakeState(user_id, client_ip)


def test_user_or_ip_prefers_the_account() -> None:
    request = _FakeRequest(user_id="alice", client_ip="1.2.3.4")

    assert resolve_identity(request, "user_or_ip") == "u:alice"  # type: ignore[arg-type]


def test_user_or_ip_falls_back_to_the_ip() -> None:
    """Carrier NAT means many Kenyan users share an IP, so IP-scoped limits must
    stay generous; but an anonymous caller must not escape limits entirely."""
    request = _FakeRequest(client_ip="1.2.3.4")

    assert resolve_identity(request, "user_or_ip") == "ip:1.2.3.4"  # type: ignore[arg-type]


def test_user_scope_on_an_anonymous_request_is_a_programming_error() -> None:
    """Silently degrading to per-IP would make a per-account limit meaningless
    without anyone noticing."""
    request = _FakeRequest()

    with pytest.raises(RuntimeError):
        resolve_identity(request, "user")  # type: ignore[arg-type]


def test_missing_client_ip_still_yields_an_identity() -> None:
    request = _FakeRequest()

    assert resolve_identity(request, "ip") == "ip:unknown"  # type: ignore[arg-type]
