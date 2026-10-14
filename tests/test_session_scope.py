"""The request's session commits before the response is sent.

FastAPI's default exit point for a dependency with `yield` is *after* the response
has gone out, so a `Depends(get_session)` commits after the client has already
been told it succeeded. That was every route until Phase 3; see `core.db.SessionDep`.

The in-process test client cannot see the difference — it returns only once the
whole application call has finished — so this checks the wiring instead. The
live two-worker test sees it directly: a job queued after commit must already be
in Redis when the client gets its response.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute

from app.core.db import get_session


def _session_scopes(dependant: Dependant, path: str) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    for sub in dependant.dependencies:
        if sub.call is get_session:
            found.append((path, sub.scope))
        found.extend(_session_scopes(sub, path))
    return found


def _routes(app: FastAPI) -> list[APIRoute | APIWebSocketRoute]:
    """Every route, through included routers.

    FastAPI 0.143 mounts an included router as a wrapper holding the original
    `APIRouter`; the original's routes carry the dependencies, which is all this
    needs. Paths are as declared, without mount prefixes.
    """
    collected: list[APIRoute | APIWebSocketRoute] = []

    def walk(routes: list[object]) -> None:
        for route in routes:
            if isinstance(route, APIRoute | APIWebSocketRoute):
                collected.append(route)
            original = getattr(route, "original_router", None)
            if original is not None:
                walk(list(original.routes))

    walk(list(app.router.routes))
    return collected


def test_every_route_commits_before_responding(shared_app: FastAPI) -> None:
    scopes = [
        found
        for route in _routes(shared_app)
        for found in _session_scopes(route.dependant, route.path)
    ]

    assert len({path for path, _ in scopes}) > 40, "the walk found too few routes to trust"
    request_scoped = sorted({path for path, scope in scopes if scope != "function"})
    assert request_scoped == []


def test_the_socket_does_not_use_the_request_session(shared_app: FastAPI) -> None:
    """It opens short sessions of its own (decision 3.5)."""
    sockets = [route for route in _routes(shared_app) if isinstance(route, APIWebSocketRoute)]

    assert [route.path for route in sockets] == ["/ws"]
    assert _session_scopes(sockets[0].dependant, "/v1/ws") == []
