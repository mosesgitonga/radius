"""The realtime socket, `/v1/ws`.

**Authentication.** The access token goes in the `Authorization` header, or — for
clients that cannot set headers on a WebSocket — in a first frame,
`{"type": "auth", "token": "..."}`, which must arrive within a few seconds. Never
in the query string: URLs are written to proxy and load-balancer logs, and an
access token in a log is a session anyone with the logs can use.

**Lifetime.** A socket lives no longer than the token it was opened with. Before it
expires the client sends another `auth` frame with a fresh token; if it does not,
the socket closes with 4401 and the client reconnects. A revoked session — signing
out, "sign out everywhere", a stolen refresh token detected — closes its sockets
at once, through a `session.revoked` event, and as a backstop the session is
re-checked every few heartbeats in case that event was missed.

**Frames from the server** are `{"type": ..., "data": {...}}`: `ready`, `pong`,
`message.created`, `message.deleted`, `conversation.updated`, `conversation.read`,
`notification.created`, `error`. A message event carries the conversation and
sequence number; the client fetches the message itself. That way a missed event
and a reconnect are handled by the same code — fetch after the last seq held.

**Close codes:** 4401 not (or no longer) authenticated, 4403 account banned,
1008 too many frames, 1013 the client fell too far behind and should reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketState

from app.core.config import Settings, get_settings
from app.core.db import session_scope
from app.core.errors import ForbiddenError, UnauthenticatedError
from app.core.realtime import SESSION_REVOKED, get_hub, mark_offline, mark_online
from app.core.redis import get_redis
from app.modules.auth.dependencies import BEARER_PREFIX, Authenticated, authenticate_access_token
from app.modules.users.service import touch_last_active

logger = logging.getLogger(__name__)

router = APIRouter(tags=["messaging"])

CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_POLICY = 1008
CLOSE_TRY_AGAIN = 1013

SEND_QUEUE_SIZE = 256
"""Events buffered per socket. A client this far behind is better served by
reconnecting and syncing than by the server holding its backlog."""

FRAMES_PER_MINUTE = 120
SESSION_RECHECK_TICKS = 4

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def get_socket_session_factory() -> SessionFactory:
    """How the socket opens database sessions: one short unit of work per check.

    Not `get_session`, which would hold one transaction open for as long as the
    socket lives — hours, on a phone left open. A dependency so tests can supply
    their own.
    """
    return session_scope


class _Close:
    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason


class SocketConnection:
    """One authenticated socket, as the hub sees it."""

    def __init__(self, websocket: WebSocket, authenticated: Authenticated) -> None:
        self.websocket = websocket
        self.connection_id = uuid.uuid4().hex
        self.user_id = authenticated.user.id
        self.session_id = authenticated.device_session.id
        self.expires_at = authenticated.claims.expires_at
        self._queue: asyncio.Queue[dict[str, Any] | _Close] = asyncio.Queue(SEND_QUEUE_SIZE)
        self._closing = False

    def deliver(self, event: dict[str, Any]) -> None:
        if self._closing:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.info("socket_slow_consumer")
            self.close(CLOSE_TRY_AGAIN, "slow_consumer")

    def close(self, code: int, reason: str) -> None:
        """Ask the writer to close. Whatever was still queued is dropped — the
        client syncs on reconnect."""
        if self._closing:
            return
        self._closing = True
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(_Close(code, reason))

    def adopt(self, authenticated: Authenticated) -> None:
        self.session_id = authenticated.device_session.id
        self.expires_at = authenticated.claims.expires_at

    async def writer(self) -> None:
        while True:
            item = await self._queue.get()
            if isinstance(item, _Close):
                await self.websocket.close(code=item.code, reason=item.reason)
                return
            if item.get("type") == SESSION_REVOKED:
                revoked = item.get("data", {}).get("session_ids", [])
                if str(self.session_id) in revoked:
                    await self.websocket.close(code=CLOSE_UNAUTHENTICATED, reason="session_revoked")
                    return
                continue
            await self.websocket.send_json(item)


def _ready_frame(connection: SocketConnection, settings: Settings) -> dict[str, Any]:
    return {
        "type": "ready",
        "data": {
            "user_id": str(connection.user_id),
            "heartbeat_seconds": settings.ws_heartbeat_seconds,
            "expires_at": connection.expires_at.isoformat(),
        },
    }


async def _authenticate(
    session_factory: SessionFactory, token: str, settings: Settings
) -> Authenticated:
    async with session_factory() as session:
        authenticated = await authenticate_access_token(session, token=token, settings=settings)
        await touch_last_active(session, authenticated.user.id)
        return authenticated


async def _still_valid(session_factory: SessionFactory, connection: SocketConnection) -> bool:
    from app.modules.auth import service as auth_service

    async with session_factory() as session:
        device_session = await auth_service.get_session_for_access(
            session, session_id=connection.session_id
        )
        return device_session is not None and device_session.revoked_at is None


def _header_token(websocket: WebSocket) -> str | None:
    header = websocket.headers.get("authorization")
    if not header or not header.startswith(BEARER_PREFIX):
        return None
    return header[len(BEARER_PREFIX) :].strip() or None


async def _first_frame_token(websocket: WebSocket, settings: Settings) -> str | None:
    try:
        frame = await asyncio.wait_for(
            websocket.receive_json(), timeout=settings.ws_auth_timeout_seconds
        )
    except (TimeoutError, WebSocketDisconnect, ValueError, KeyError):
        return None
    if not isinstance(frame, dict) or frame.get("type") != "auth":
        return None
    token = frame.get("token")
    return token if isinstance(token, str) and token else None


@router.websocket("/ws")
async def realtime_socket(
    websocket: WebSocket,
    session_factory: Annotated[SessionFactory, Depends(get_socket_session_factory)],
) -> None:
    settings = get_settings()
    await websocket.accept()

    token = _header_token(websocket) or await _first_frame_token(websocket, settings)
    if token is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED, reason="authentication_required")
        return
    try:
        authenticated = await _authenticate(session_factory, token, settings)
    except UnauthenticatedError:
        await websocket.close(code=CLOSE_UNAUTHENTICATED, reason="invalid_token")
        return
    except ForbiddenError:
        await websocket.close(code=CLOSE_FORBIDDEN, reason="account_banned")
        return

    connection = SocketConnection(websocket, authenticated)
    hub = get_hub()
    redis = get_redis()
    await hub.register(connection)
    await mark_online(
        redis,
        connection.user_id,
        connection.connection_id,
        ttl_seconds=settings.presence_ttl_seconds,
    )
    logger.info("socket_connected")
    connection.deliver(_ready_frame(connection, settings))

    tasks = [
        asyncio.create_task(connection.writer(), name="socket-writer"),
        asyncio.create_task(_reader(connection, session_factory, settings), name="socket-reader"),
        asyncio.create_task(_ticker(connection, session_factory, settings), name="socket-ticker"),
    ]
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
    finally:
        await hub.unregister(connection)
        await mark_offline(redis, connection.user_id, connection.connection_id)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()
        logger.info("socket_disconnected")


async def _reader(
    connection: SocketConnection, session_factory: SessionFactory, settings: Settings
) -> None:
    window_start = time.monotonic()
    frames = 0
    while True:
        try:
            frame = await connection.websocket.receive_json()
        except WebSocketDisconnect:
            return
        except (ValueError, KeyError):
            connection.deliver({"type": "error", "data": {"code": "invalid_frame"}})
            continue

        now = time.monotonic()
        if now - window_start >= 60:
            window_start, frames = now, 0
        frames += 1
        if frames > FRAMES_PER_MINUTE:
            connection.close(CLOSE_POLICY, "too_many_frames")
            return

        kind = frame.get("type") if isinstance(frame, dict) else None
        if kind == "ping":
            connection.deliver({"type": "pong", "data": {}})
        elif kind == "auth":
            await _reauthenticate(connection, frame, session_factory, settings)
        else:
            connection.deliver({"type": "error", "data": {"code": "unknown_frame"}})


async def _reauthenticate(
    connection: SocketConnection,
    frame: dict[str, Any],
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    """Extend the socket with a fresh token. It must be the same person: a socket
    is never handed from one account to another."""
    token = frame.get("token")
    if not isinstance(token, str) or not token:
        connection.deliver({"type": "error", "data": {"code": "invalid_frame"}})
        return
    try:
        authenticated = await _authenticate(session_factory, token, settings)
    except (UnauthenticatedError, ForbiddenError):
        connection.close(CLOSE_UNAUTHENTICATED, "invalid_token")
        return
    if authenticated.user.id != connection.user_id:
        connection.close(CLOSE_UNAUTHENTICATED, "different_user")
        return
    connection.adopt(authenticated)
    connection.deliver(_ready_frame(connection, settings))


async def _ticker(
    connection: SocketConnection, session_factory: SessionFactory, settings: Settings
) -> None:
    """Keeps presence fresh, enforces token expiry, and re-checks the session."""
    redis = get_redis()
    ticks = 0
    while True:
        until_expiry = (connection.expires_at - datetime.now(UTC)).total_seconds()
        await asyncio.sleep(max(0.05, min(settings.ws_heartbeat_seconds, until_expiry)))
        if datetime.now(UTC) >= connection.expires_at:
            connection.close(CLOSE_UNAUTHENTICATED, "token_expired")
            return
        await mark_online(
            redis,
            connection.user_id,
            connection.connection_id,
            ttl_seconds=settings.presence_ttl_seconds,
        )
        ticks += 1
        if ticks % SESSION_RECHECK_TICKS == 0 and not await _still_valid(
            session_factory, connection
        ):
            connection.close(CLOSE_UNAUTHENTICATED, "session_revoked")
            return
