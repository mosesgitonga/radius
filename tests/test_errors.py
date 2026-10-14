"""The error envelope, end to end.

Clients switch on `error.code`, so the envelope's shape is part of the API
contract and is pinned here rather than left to each handler.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel

from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    ErrorCode,
    ForbiddenError,
    InvalidCursorError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    UnauthenticatedError,
)


class _Point(BaseModel):
    latitude: float
    longitude: float


@pytest.fixture
def error_routes(fresh_app: FastAPI) -> FastAPI:
    app = fresh_app
    """Routes that raise each kind of failure, mounted on the test application."""

    @app.get("/v1/test/not-found")
    async def not_found() -> None:
        raise NotFoundError("Post not found", details={"post_id": "abc"})

    @app.get("/v1/test/forbidden")
    async def forbidden() -> None:
        raise ForbiddenError

    @app.get("/v1/test/unauthenticated")
    async def unauthenticated() -> None:
        raise UnauthenticatedError

    @app.get("/v1/test/conflict")
    async def conflict() -> None:
        raise ConflictError("Already sold")

    @app.get("/v1/test/bad-request")
    async def bad_request() -> None:
        raise BadRequestError("Phone number is not a Kenyan mobile number")

    @app.get("/v1/test/invalid-cursor")
    async def invalid_cursor() -> None:
        raise InvalidCursorError

    @app.get("/v1/test/rate-limited")
    async def rate_limited() -> None:
        raise RateLimitedError(retry_after_seconds=42)

    @app.get("/v1/test/unavailable")
    async def unavailable() -> None:
        raise ServiceUnavailableError

    @app.get("/v1/test/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail: db password is hunter2")

    @app.post("/v1/test/point")
    async def point(body: _Point) -> _Point:
        return body

    return app


@pytest_asyncio.fixture(loop_scope="session")
async def error_client(error_routes: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Client that returns the 500 response instead of re-raising, so the
    unhandled-exception path can be inspected the way a real client sees it."""
    transport = httpx.ASGITransport(app=error_routes, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_code"),
    [
        ("/v1/test/not-found", 404, "not_found"),
        ("/v1/test/forbidden", 403, "forbidden"),
        ("/v1/test/unauthenticated", 401, "unauthenticated"),
        ("/v1/test/conflict", 409, "conflict"),
        ("/v1/test/bad-request", 400, "bad_request"),
        ("/v1/test/invalid-cursor", 400, "invalid_cursor"),
        ("/v1/test/rate-limited", 429, "rate_limited"),
        ("/v1/test/unavailable", 503, "service_unavailable"),
    ],
)
async def test_each_error_maps_to_its_status_and_code(
    error_client: httpx.AsyncClient,
    path: str,
    expected_status: int,
    expected_code: str,
) -> None:
    response = await error_client.get(path)

    assert response.status_code == expected_status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == expected_code


async def test_details_reach_the_client(error_client: httpx.AsyncClient) -> None:
    response = await error_client.get("/v1/test/not-found")

    assert response.json()["error"]["details"] == {"post_id": "abc"}


async def test_rate_limited_sets_retry_after(error_client: httpx.AsyncClient) -> None:
    """Mobile clients need a number to back off by, in a header they can read
    without parsing the body."""
    response = await error_client.get("/v1/test/rate-limited")

    assert response.headers["Retry-After"] == "42"
    assert response.json()["error"]["details"]["retry_after_seconds"] == 42


async def test_unauthenticated_advertises_the_scheme(
    error_client: httpx.AsyncClient,
) -> None:
    response = await error_client.get("/v1/test/unauthenticated")

    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_unhandled_exceptions_leak_nothing(
    error_client: httpx.AsyncClient,
) -> None:
    """A stack trace tells an attacker the library versions and file layout, and
    this one would hand over a password. Debugging goes in the logs instead,
    joined up by the request ID."""
    response = await error_client.get("/v1/test/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "Something went wrong"
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


async def test_failures_still_carry_the_request_id(
    error_client: httpx.AsyncClient,
) -> None:
    """Including the 500 path, which Starlette handles outside our middleware —
    without this, the one failure a user is most likely to report is the one
    that cannot be traced."""
    for path in ("/v1/test/not-found", "/v1/test/boom"):
        response = await error_client.get(path)
        assert response.headers.get("X-Request-ID"), path


async def test_validation_errors_report_the_field_but_not_the_value(
    error_client: httpx.AsyncClient,
) -> None:
    """Pydantic includes the offending input by default. Here that would put raw
    coordinates into an error payload and, worse, into the logs — which the
    brief forbids outright. Only the location and reason are reported."""
    response = await error_client.post(
        "/v1/test/point",
        json={"latitude": "not-a-number", "longitude": -0.4201341},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    fields = body["error"]["details"]["fields"]
    assert fields[0]["location"] == ["body", "latitude"]
    assert "not-a-number" not in response.text
    assert "-0.4201341" not in response.text


async def test_missing_body_is_a_validation_error(
    error_client: httpx.AsyncClient,
) -> None:
    response = await error_client.post("/v1/test/point", json={})

    assert response.status_code == 422
    locations = [f["location"] for f in response.json()["error"]["details"]["fields"]]
    assert ["body", "latitude"] in locations
    assert ["body", "longitude"] in locations


async def test_wrong_method_uses_the_envelope(error_client: httpx.AsyncClient) -> None:
    response = await error_client.delete("/v1/test/not-found")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_error_codes_are_unique() -> None:
    """Reusing a code's value would silently change what clients branch on."""
    values = [member.value for member in ErrorCode]

    assert len(values) == len(set(values))


def test_app_error_defaults_can_be_overridden_per_call_site() -> None:
    error = AppError("custom message", details={"field": "value"})

    assert error.message == "custom message"
    assert error.details == {"field": "value"}
    assert error.to_envelope().error.code == ErrorCode.internal_error


def test_subclass_messages_default_sensibly() -> None:
    assert NotFoundError().message
    assert ForbiddenError().to_envelope().error.code == ErrorCode.forbidden
