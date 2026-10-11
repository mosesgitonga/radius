"""HTTP middleware: request IDs and the access log.

Written as pure ASGI middleware rather than Starlette's `BaseHTTPMiddleware`
because this also has to cover the WebSocket connections that arrive in Phase 3,
and because `BaseHTTPMiddleware` wraps every response in an extra task, which
complicates streaming and exception propagation for no gain here.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.context import (
    REQUEST_ID_HEADER,
    new_request_id,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger("app.access")

# An inbound request ID is echoed in a response header and written to logs, so it
# is validated rather than trusted: no CR/LF (header injection), nothing unbounded.
_SAFE_REQUEST_ID: Final = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

_LOCAL_SCOPE_FALLBACK: Final = "unknown"


def _sanitise_request_id(value: str | None) -> str | None:
    if value and _SAFE_REQUEST_ID.match(value):
        return value
    return None


def client_ip_from_scope(scope: Scope, *, trust_forwarded_for: bool) -> str:
    """Best-effort client IP, used for per-IP rate limits and abuse tracing.

    `X-Forwarded-For` is only consulted when configured, because any client can
    send that header. Behind a correctly configured reverse proxy the leftmost
    entry is the real client; without one, trusting it would let a caller forge a
    fresh IP per request and walk straight through the rate limiter.
    """
    if trust_forwarded_for:
        forwarded = Headers(scope=scope).get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    client = scope.get("client")
    if client:
        return str(client[0])
    return _LOCAL_SCOPE_FALLBACK


class RequestContextMiddleware:
    """Assigns a request ID, echoes it back, and logs one line per request."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self._excluded = settings.access_log_excluded_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER.lower())
        request_id = _sanitise_request_id(inbound) or new_request_id()
        token = set_request_id(request_id)

        # Handlers and dependencies read these off `request.state`.
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["client_ip"] = client_ip_from_scope(
            scope, trust_forwarded_for=self.settings.trust_forwarded_for
        )

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = message.setdefault("headers", [])
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Starlette's ServerErrorMiddleware turns this into a 500 response and
            # the exception handler logs the traceback; here we only make sure the
            # access line records the failure before re-raising.
            self._log(scope, 500, started)
            raise
        else:
            self._log(scope, status_code, started)
        finally:
            reset_request_id(token)

    def _log(self, scope: Scope, status_code: int, started: float) -> None:
        if not self.settings.log_access:
            return
        path = str(scope.get("path", ""))
        if path in self._excluded:
            return

        # The query string is deliberately not logged: it can carry coordinates on
        # location endpoints, and the brief forbids those reaching the logs.
        logger.info(
            "http_request",
            extra={
                "method": scope.get("method", scope["type"]),
                "path": path,
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "client_ip": scope.get("state", {}).get("client_ip"),
            },
        )
