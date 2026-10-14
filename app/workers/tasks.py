"""Background tasks.

Tasks are plain async functions taking arq's `ctx` as their first argument, which
means tests call them directly with a stub context instead of standing up a
worker — the approach the brief asks for.

Each task here is a thin wrapper: it opens a session and calls the module that
owns the work. The scheduling lives in `app/workers/settings.py`, the logic lives
with the models it touches, and this file is only the seam between them.

Later phases add event reminders (Phase 7) and badge recomputation (Phase 8).
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Concatenate, cast

from arq.typing import WorkerCoroutine
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import new_request_id, reset_request_id, set_request_id
from app.core.db import session_scope
from app.core.idempotency import purge_expired as purge_idempotency_records
from app.core.providers.push import get_push_provider
from app.core.providers.storage import get_storage_provider
from app.core.redis import get_redis
from app.modules.feed.tasks import expire_stories
from app.modules.location.tasks import refresh_cell_activity, roll_home_presence
from app.modules.media import service as media_service
from app.modules.messaging import service as messaging_service
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users.tasks import delete_expired_sessions, purge_deleted_accounts

logger = logging.getLogger(__name__)

JobContext = dict[Any, Any]
"""arq's job context. Carries `job_id`, `job_try`, the Redis pool, and
whatever `on_startup` put there."""


def with_job_context[**P, R](
    func: Callable[Concatenate[JobContext, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[JobContext, P], Coroutine[Any, Any, R]]:
    """Give a task a correlation ID and log its start, finish, and failures.

    The job ID is reused as the request ID so that work enqueued by a request can
    be traced through the worker's logs with the same field the API uses.
    """

    @functools.wraps(func)
    # `ctx` is positional-only so the wrapper's type matches Concatenate,
    # which functools.wraps would otherwise widen into a named parameter.
    async def wrapper(ctx: JobContext, /, *args: P.args, **kwargs: P.kwargs) -> R:
        job_id = str(ctx.get("job_id") or new_request_id())
        token = set_request_id(job_id)
        started = datetime.now(UTC)
        logger.info("task_started", extra={"task": func.__name__})
        try:
            result = await func(ctx, *args, **kwargs)
        except Exception:
            logger.exception("task_failed", extra={"task": func.__name__})
            raise
        else:
            logger.info(
                "task_finished",
                extra={
                    "task": func.__name__,
                    "duration_ms": round((datetime.now(UTC) - started).total_seconds() * 1000, 2),
                },
            )
            return result
        finally:
            reset_request_id(token)

    return wrapper


@asynccontextmanager
async def task_session(ctx: JobContext) -> AsyncIterator[AsyncSession]:
    """The session a task works in.

    In production, a fresh unit of work per job. A test may put its own session in
    `ctx["session"]`, so it can run the real task against data inside the test's
    transaction — which a separate connection could never see — instead of
    bypassing the task and calling the service underneath. The task code under
    test is then the task code that runs.
    """
    provided = ctx.get("session")
    if isinstance(provided, AsyncSession):
        yield provided
        await provided.flush()
        return
    async with session_scope() as session:
        yield session


@with_job_context
async def heartbeat(ctx: JobContext) -> str:
    """Prove the worker is alive and its schedule fires.

    Kept now that real tasks exist, because it is the only scheduled job with no
    dependencies: if the heartbeat stops while other jobs are failing, the
    problem is the worker, and if it continues, the problem is the job.
    """
    now = datetime.now(UTC)
    logger.info("worker_heartbeat", extra={"at": now.isoformat()})
    return now.isoformat()


@with_job_context
async def refresh_density(ctx: JobContext) -> dict[str, int]:
    """Recompute `cell_activity`, which is what makes the radius adaptive.

    Every fifteen minutes. If this stops, radii do not break — they freeze at
    their last values and slowly drift out of step with reality, which is why
    Phase 8 alerts on the table's staleness rather than only on job failures.
    """
    settings = get_settings()
    async with task_session(ctx) as session:
        result = await refresh_cell_activity(session, settings=settings)
    return {
        "cells_updated": result.cells_updated,
        "stale_cells_removed": result.stale_cells_removed,
    }


@with_job_context
async def roll_presence(ctx: JobContext) -> dict[str, int]:
    """Age home-presence masks forward for accounts that missed a day."""
    async with task_session(ctx) as session:
        rolled = await roll_home_presence(session)
    return {"users_updated": rolled}


@with_job_context
async def run_retention(ctx: JobContext) -> dict[str, int]:
    """Purge accounts past their grace period, and sweep stale sessions.

    One job rather than two, because both are daily retention work over the same
    tables and splitting them would double the schedule for no benefit.
    """
    settings = get_settings()
    storage = get_storage_provider()
    async with task_session(ctx) as session:
        purged = await purge_deleted_accounts(session, settings=settings, storage=storage)
        sessions_removed = await delete_expired_sessions(session)
        uploads_swept = await media_service.sweep_abandoned_uploads(
            session, storage, settings=settings
        )
        idempotency_purged = await purge_idempotency_records(session, settings=settings)
        notifications_purged = await notifications_service.purge_old_notifications(
            session, settings=settings
        )
        tokens_removed = await notifications_service.delete_stale_device_tokens(
            session, settings=settings
        )
    return {
        "accounts_purged": purged.accounts_purged,
        "expired_sessions_deleted": sessions_removed,
        "abandoned_uploads_swept": uploads_swept,
        "idempotency_records_purged": idempotency_purged,
        "notifications_purged": notifications_purged,
        "stale_device_tokens_deleted": tokens_removed,
    }


@with_job_context
async def process_media(ctx: JobContext, media_id: str) -> str:
    """Process one upload: validate, strip metadata, render variants.

    Enqueued when a client confirms an upload. Returns the asset's final status,
    which arq keeps for an hour — handy when diagnosing a stuck upload.
    """
    settings = get_settings()
    storage = get_storage_provider()
    async with task_session(ctx) as session:
        asset = await media_service.get_asset(session, uuid.UUID(media_id))
        if asset is None:
            logger.warning("process_media_missing_asset", extra={"media_id": media_id})
            return "missing"
        if asset.status.value != "processing":
            # Already done — a duplicate enqueue, or the catch-up sweep got there
            # first. Processing twice would delete the variants it just made.
            return asset.status.value
        asset = await media_service.process_asset(session, storage, asset=asset, settings=settings)
        return asset.status.value


@with_job_context
async def catch_up_media(ctx: JobContext) -> dict[str, int]:
    """Process uploads whose job was lost.

    Enqueueing is best-effort by design, so a Redis blip can drop a job. Every few
    minutes this finds assets still `processing` after a grace period and does the
    work, so a lost job means a late image rather than one stuck for ever.
    """
    settings = get_settings()
    storage = get_storage_provider()
    processed = 0
    async with task_session(ctx) as session:
        stuck = await media_service.assets_awaiting_processing(
            session, older_than=timedelta(minutes=2)
        )
        for asset in stuck:
            await media_service.process_asset(session, storage, asset=asset, settings=settings)
            processed += 1
    return {"processed": processed}


@with_job_context
async def expire_old_stories(ctx: JobContext) -> dict[str, int]:
    """Delete stories past their 24 hours, and their images."""
    storage = get_storage_provider()
    async with task_session(ctx) as session:
        result = await expire_stories(session, storage)
    return {"deleted": result.deleted, "held_for_review": result.held_for_review}


@with_job_context
async def deliver_message_push(ctx: JobContext, message_id: str) -> dict[str, int]:
    """Push a new message to the members who are not connected.

    Enqueued after the message commits. Presence is checked here, at delivery,
    not when the message was sent: someone who opened the app in between has
    already seen it.
    """
    settings = get_settings()
    async with task_session(ctx) as session:
        return await messaging_service.deliver_message_push(
            session,
            get_redis(),
            get_push_provider(),
            message_id=uuid.UUID(message_id),
            settings=settings,
        )


@with_job_context
async def deliver_notification_push(
    ctx: JobContext,
    recipient_id: str,
    kind: str,
    actor_id: str,
    collapse_key: str,
    data: dict[str, str],
) -> str:
    """Push one activity notification — a comment, reply, reaction, request or
    invitation. Returns why it was or was not sent, which arq keeps for an hour."""
    settings = get_settings()
    async with task_session(ctx) as session:
        outcome = await notifications_service.deliver_notification_push(
            session,
            get_redis(),
            get_push_provider(),
            recipient_id=uuid.UUID(recipient_id),
            kind=NotificationType(kind),
            actor_id=uuid.UUID(actor_id),
            collapse_key=collapse_key,
            data=data,
            settings=settings,
        )
    return outcome.reason


def as_worker_task(func: Callable[..., Coroutine[Any, Any, Any]]) -> WorkerCoroutine:
    """Narrow a task to the type arq registers.

    arq's `WorkerCoroutine` protocol demands an open `(ctx, *args, **kwargs)`
    signature. Our tasks declare their real parameters instead, which is what
    makes them worth calling directly from tests — so the cast happens once,
    here, rather than as a type-ignore at each registration.
    """
    return cast("WorkerCoroutine", func)
