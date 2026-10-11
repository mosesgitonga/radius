"""Liveness, readiness, and the error envelope as served over HTTP."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.core import health as health_module


async def test_health_reports_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"


async def test_health_does_not_touch_dependencies(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness must stay green when a dependency is down.

    Otherwise an orchestrator restarts perfectly healthy containers every time
    Redis hiccups, turning a degraded dependency into an outage.
    """

    async def unreachable() -> bool:
        raise AssertionError("liveness must not check dependencies")

    monkeypatch.setattr(health_module, "check_database", unreachable)
    monkeypatch.setattr(health_module, "check_redis", unreachable)

    assert (await client.get("/health")).status_code == 200


async def test_ready_reports_both_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": True, "redis": True},
    }


async def test_ready_returns_503_when_redis_is_down(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def redis_down() -> bool:
        return False

    monkeypatch.setattr(health_module, "check_redis", redis_down)

    response = await client.get("/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "service_unavailable"
    # The individual results are reported so an operator knows which dependency
    # to look at without reading logs.
    assert error["details"]["checks"] == {"database": True, "redis": False}


async def test_every_response_carries_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    request_id = response.headers.get("X-Request-ID")
    assert request_id
    assert len(request_id) == 32


async def test_inbound_request_id_is_honoured(client: httpx.AsyncClient) -> None:
    """A client-supplied ID is reused so mobile and server logs line up."""
    response = await client.get("/health", headers={"X-Request-ID": "client-abc-123"})

    assert response.headers["X-Request-ID"] == "client-abc-123"


@pytest.mark.parametrize(
    "malicious",
    [
        "has spaces",
        "inject\r\nX-Evil: yes",
        "x" * 200,
        "",
    ],
)
async def test_unsafe_request_ids_are_replaced(client: httpx.AsyncClient, malicious: str) -> None:
    """An inbound ID is echoed into a header, so it is validated, not trusted.

    httpx rejects outright illegal header values itself, so the ones that reach
    the server are the plausible-looking ones; those must still be replaced.
    """
    try:
        response = await client.get("/health", headers={"X-Request-ID": malicious})
    except (httpx.LocalProtocolError, ValueError):
        pytest.skip("client refused to send this header value")

    echoed = response.headers["X-Request-ID"]
    assert echoed != malicious
    assert len(echoed) == 32


async def test_unknown_route_uses_the_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == "not_found"


async def test_openapi_schema_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert "/health" in schema["paths"]
    assert "/ready" in schema["paths"]


async def test_cors_is_off_by_default(app: FastAPI) -> None:
    """The clients are native apps and send no Origin header.

    Enabling CORS by default would hand any website the ability to call this API
    with a user's credentials for no benefit.
    """
    middleware_names = {
        getattr(entry.cls, "__name__", str(entry.cls)) for entry in app.user_middleware
    }

    assert "CORSMiddleware" not in middleware_names
