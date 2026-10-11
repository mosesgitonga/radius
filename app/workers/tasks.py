"""Background tasks.

Tasks are plain async functions taking arq's `ctx` as their first argument, which
means tests call them directly with a stub context instead of standing up a
worker — the approach the brief asks for.

Later phases register their work here: the `cell_activity` density refresh and
radius recomputation (Phase 1), story expiry (Phase 2), push fan-out (Phase 3),
event reminders (Phase 7), badge recomputation and retention purges (Phase 8).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Concatenate, cast

from arq.typing import WorkerCoroutine

from app.core.context import new_request_id, reset_request_id, set_request_id

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

    Scheduled on the same fifteen-minute cadence the `cell_activity` refresh will
    use in Phase 1, so that if the schedule is misconfigured we find out now rather
    than when the radius engine depends on it.
    """
    now = datetime.now(UTC)
    logger.info("worker_heartbeat", extra={"at": now.isoformat()})
    return now.isoformat()


def as_worker_task(func: Callable[..., Coroutine[Any, Any, Any]]) -> WorkerCoroutine:
    """Narrow a task to the type arq registers.

    arq's `WorkerCoroutine` protocol demands an open `(ctx, *args, **kwargs)`
    signature. Our tasks declare their real parameters instead, which is what
    makes them worth calling directly from tests — so the cast happens once,
    here, rather than as a type-ignore at each registration.
    """
    return cast("WorkerCoroutine", func)
