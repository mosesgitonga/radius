"""Idempotent create endpoints.

A client on a Kenyan mobile connection sends `POST /v1/posts`, the connection drops
before the response arrives, and the client retries. Without this, the neighbour
sees the post twice and the author cannot tell which copy to delete.

The `Idempotency-Key` header fixes it: the first request's response is stored
against the key and replayed for any repeat. The client generates a fresh UUID per
logical action, so a retry carries the same key and a genuinely new post carries a
new one.

**Why this is in `core` and owns a table.** It is the same kind of cross-cutting
concern as rate limiting — every module's create endpoints use it, and none of them
should implement it. The table is here rather than in a module because no module
owns it.

**Why PostgreSQL and not Redis.** The stored response has to be as durable as the
thing it describes. If Redis is flushed, a replay would create a second post, which
is precisely the outcome this exists to prevent; and the unique index gives the
concurrency guarantee below for free.

**Concurrency.** Two simultaneous requests with one key are not hypothetical — a
client retrying on a timeout often has the first request still in flight. The key is
claimed by an INSERT against a unique index, so exactly one request proceeds and the
other is told to retry rather than being allowed to duplicate the work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import DateTime, Index, Integer, String, Uuid, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, ErrorCode
from app.core.models import Base, UUIDPrimaryKeyMixin

logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
MAX_KEY_LENGTH: Final = 255


class IdempotencyKeyReusedError(AppError):
    """The same key was sent with a different request body.

    A 422, not a 409: the key is not in conflict with anything, the *client* has a
    bug — it reused a key for a different action, which means its retry logic is
    broken in a way that would eventually lose someone's post.
    """

    status_code = 422
    code = ErrorCode.validation_error
    message = (
        "This Idempotency-Key was already used with a different request. "
        "Use a new key for each distinct action."
    )


class IdempotencyInProgressError(ConflictError):
    message = "A request with this Idempotency-Key is still being processed"


class IdempotencyRecord(Base, UUIDPrimaryKeyMixin):
    """One client-supplied key, its request fingerprint, and the response given."""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    """Keys are scoped per user, so one client's choice of key cannot collide with
    another's — and cannot be used to probe whether somebody else's key exists."""

    endpoint: Mapped[str] = mapped_column(String(255))
    """Also part of the identity. The same key on a different endpoint is a
    different action, and treating them as one would replay a post's response to a
    request that meant to create a listing."""

    request_fingerprint: Mapped[str] = mapped_column(String(64))
    """SHA-256 of the body. Lets a reused key with changed content be rejected
    rather than silently answered with the wrong response."""

    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """Null while the first request is still running. That is what distinguishes
    "in progress" from "complete" without a separate state column."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The claim: this unique index is what makes the whole thing concurrency-safe.
        Index(
            "uq_idempotency_records_user_endpoint_key",
            "user_id",
            "endpoint",
            "key",
            unique=True,
        ),
        Index("ix_idempotency_records_created_at", "created_at"),
    )


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


def fingerprint_payload(payload: Any) -> str:
    """A stable hash of a request body.

    `sort_keys` so that a client reordering its JSON fields does not look like a
    different request.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def read_key(request: Request) -> str | None:
    """The header, if present and plausible.

    An over-long key is ignored rather than rejected: the worst case is that the
    request is simply not idempotent, which is better than refusing to create a
    post because a client's UUID library is unusual.
    """
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not key:
        return None
    key = key.strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        logger.info("idempotency_key_ignored", extra={"reason": "malformed"})
        return None
    return key


async def claim(
    session: AsyncSession,
    *,
    key: str,
    user_id: uuid.UUID,
    endpoint: str,
    payload: Any,
    settings: Settings,
) -> StoredResponse | None:
    """Claim a key, or return the response already stored against it.

    Returns `None` when the caller should go ahead and do the work, then call
    `complete`. Returns a `StoredResponse` when this is a retry and the original
    answer should be replayed.

    Raises `IdempotencyInProgressError` if the first request is still running, and
    `IdempotencyKeyReusedError` if the key arrives with a different body.

    The claim is committed immediately. That is deliberate and slightly unusual:
    the row has to be visible to a concurrent request *before* the work is done,
    which is the whole point of claiming it.
    """
    fingerprint = fingerprint_payload(payload)

    existing = await _find(session, key=key, user_id=user_id, endpoint=endpoint)
    if existing is None:
        record = IdempotencyRecord(
            key=key,
            user_id=user_id,
            endpoint=endpoint,
            request_fingerprint=fingerprint,
        )
        session.add(record)
        try:
            await session.commit()
        except IntegrityError:
            # Another request claimed it between the SELECT and the INSERT. Not an
            # error: look again and treat it as the retry it is.
            await session.rollback()
            existing = await _find(session, key=key, user_id=user_id, endpoint=endpoint)
            if existing is None:  # pragma: no cover — the row must exist by now
                raise
        else:
            return None

    assert existing is not None  # noqa: S101 — narrowed by the branch above

    if existing.request_fingerprint != fingerprint:
        raise IdempotencyKeyReusedError

    if existing.response_body is None:
        # Claimed but unfinished. 409 with Retry-After is the honest answer: the
        # work may yet succeed, and the client should ask again rather than
        # assuming failure and creating a duplicate under a new key.
        raise IdempotencyInProgressError(
            details={"retry_after_seconds": 2},
        )

    logger.info("idempotent_replay", extra={"endpoint": endpoint})
    return StoredResponse(status_code=existing.response_status or 200, body=existing.response_body)


async def complete(
    session: AsyncSession,
    *,
    key: str,
    user_id: uuid.UUID,
    endpoint: str,
    status_code: int,
    body: dict[str, Any],
) -> None:
    """Store the response so a retry replays it."""
    record = await _find(session, key=key, user_id=user_id, endpoint=endpoint)
    if record is None:  # pragma: no cover — claim() guarantees it
        return
    record.response_status = status_code
    record.response_body = body
    record.completed_at = datetime.now(UTC)


async def release(session: AsyncSession, *, key: str, user_id: uuid.UUID, endpoint: str) -> None:
    """Drop an unfinished claim after the work failed.

    Without this, a request that errored would leave its key claimed, and the
    client's retry would be told "in progress" for as long as the record lived —
    turning one transient failure into a permanently unusable key.
    """
    await session.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.key == key,
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.endpoint == endpoint,
            IdempotencyRecord.response_body.is_(None),
        )
    )
    await session.commit()


async def _find(
    session: AsyncSession, *, key: str, user_id: uuid.UUID, endpoint: str
) -> IdempotencyRecord | None:
    statement = select(IdempotencyRecord).where(
        IdempotencyRecord.key == key,
        IdempotencyRecord.user_id == user_id,
        IdempotencyRecord.endpoint == endpoint,
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def purge_expired(
    session: AsyncSession, *, settings: Settings, now: datetime | None = None
) -> int:
    """Delete records past their retention window.

    Run daily. The window only needs to outlast any plausible client retry; a day
    is generous for that, and keeping them longer would mean a key reused months
    later still replaying an ancient response.
    """
    from app.core.db import execute_dml

    cutoff = (now or datetime.now(UTC)) - timedelta(hours=settings.idempotency_ttl_hours)
    removed = await execute_dml(
        session, delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff)
    )
    if removed:
        logger.info("idempotency_records_purged", extra={"count": removed})
    return removed


async def execute_idempotently(
    session: AsyncSession,
    *,
    request: Request,
    user_id: uuid.UUID,
    payload: Any,
    status_code: int,
    action: Callable[[], Awaitable[BaseModel]],
    settings: Settings,
) -> JSONResponse:
    """Run a create action at most once per `Idempotency-Key`.

    Without a key, the action simply runs. With one: a first request claims the
    key, runs, and stores its response; a retry gets that stored response back
    with an `Idempotent-Replayed: true` header and nothing is created twice.

    On failure the claim is released *after* rolling back the action's work. The
    order matters: releasing commits, and committing before the rollback would
    persist half of whatever the failed action did.
    """
    endpoint = f"{request.method} {request.url.path}"
    key = read_key(request)

    if key is None:
        result = await action()
        return JSONResponse(result.model_dump(mode="json"), status_code=status_code)

    stored = await claim(
        session,
        key=key,
        user_id=user_id,
        endpoint=endpoint,
        payload=payload,
        settings=settings,
    )
    if stored is not None:
        return JSONResponse(
            stored.body,
            status_code=stored.status_code,
            headers={"Idempotent-Replayed": "true"},
        )

    try:
        result = await action()
        body = result.model_dump(mode="json")
        await complete(
            session,
            key=key,
            user_id=user_id,
            endpoint=endpoint,
            status_code=status_code,
            body=body,
        )
    except Exception:
        await session.rollback()
        await release(session, key=key, user_id=user_id, endpoint=endpoint)
        raise
    return JSONResponse(body, status_code=status_code)
