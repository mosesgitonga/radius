"""Per-request context.

A context variable holds the request ID so any code can reach it — loggers,
services, error handlers — without threading it through every call signature.
Context variables are task-local, so concurrent requests on the same worker
cannot see each other's values.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

REQUEST_ID_HEADER = "X-Request-ID"


def new_request_id() -> str:
    return uuid.uuid4().hex


def get_request_id() -> str | None:
    """The current request's ID, or None outside a request (worker tasks, startup)."""
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str | None]:
    """Bind a request ID to the current context.

    The returned token resets the previous value, which matters in tests and in
    worker tasks that handle several jobs on one task.
    """
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)
