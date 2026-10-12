"""Background tasks.

Tasks are plain async functions taking arq's `ctx` as their first argument, which
means tests call them directly with a stub context instead of standing up a
worker — the approach the brief asks for.

Each task here is a thin wrapper: it opens a session and calls the module that
owns the work. The scheduling lives in `app/workers/settings.py`, the logic lives
with the models it touches, and this file is only the seam between them.

Later phases add: story expiry (Phase 2), push fan-out (Phase 3), event reminders
(Phase 7), and badge recomputation (Phase 8).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Concatenate, cast

from arq.typing import WorkerCoroutine

from app.core.config import get_settings
from app.core.context import new_request_id, reset_request_id, set_request_id
from app.core.db import session_scope
from app.modules.location.tasks import refresh_cell_activity, roll_home_presence
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
    async with session_scope() as session:
        result = await refresh_cell_activity(session, settings=settings)
    return {
        "cells_updated": result.cells_updated,
        "stale_cells_removed": result.stale_cells_removed,
    }


@with_job_context
async def roll_presence(ctx: JobContext) -> dict[str, int]:
    """Age home-presence masks forward for accounts that missed a day."""
    async with session_scope() as session:
        rolled = await roll_home_presence(session)
    return {"users_updated": rolled}


@with_job_context
async def run_retention(ctx: JobContext) -> dict[str, int]:
    """Purge accounts past their grace period, and sweep stale sessions.

    One job rather than two, because both are daily retention work over the same
    tables and splitting them would double the schedule for no benefit.
    """
    settings = get_settings()
    async with session_scope() as session:
        purged = await purge_deleted_accounts(session, settings=settings)
        sessions_removed = await delete_expired_sessions(session)
    return {
        "accounts_purged": purged.accounts_purged,
        "expired_sessions_deleted": sessions_removed,
    }


def as_worker_task(func: Callable[..., Coroutine[Any, Any, Any]]) -> WorkerCoroutine:
    """Narrow a task to the type arq registers.

    arq's `WorkerCoroutine` protocol demands an open `(ctx, *args, **kwargs)`
    signature. Our tasks declare their real parameters instead, which is what
    makes them worth calling directly from tests — so the cast happens once,
    here, rather than as a type-ignore at each registration.
    """
    return cast("WorkerCoroutine", func)
