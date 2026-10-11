"""arq worker configuration.

Run with:

    arq app.workers.settings.WorkerSettings

One worker process handles both queued jobs and scheduled ones. That is enough at
launch scale and keeps the deployment to a single extra container; splitting
scheduled work onto its own worker is a later change that needs no code.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.typing import WorkerCoroutine

from app.core.config import get_settings
from app.core.db import dispose_engine
from app.core.logging import configure_logging
from app.core.redis import close_redis
from app.workers.tasks import as_worker_task, heartbeat

logger = logging.getLogger(__name__)


async def on_startup(ctx: dict[str, object]) -> None:
    configure_logging(get_settings(), service="worker")
    logger.info("worker_starting")


async def on_shutdown(ctx: dict[str, object]) -> None:
    await dispose_engine()
    await close_redis()
    logger.info("worker_stopped")


class WorkerSettings:
    """Picked up by the `arq` CLI."""

    functions: ClassVar[list[WorkerCoroutine]] = [as_worker_task(heartbeat)]

    cron_jobs: ClassVar[list[CronJob]] = [
        # Every quarter hour. Phase 1's cell_activity refresh joins this schedule.
        cron(
            as_worker_task(heartbeat),
            minute={0, 15, 30, 45},
            second=0,
            run_at_startup=False,
        ),
    ]

    on_startup = staticmethod(on_startup)
    on_shutdown = staticmethod(on_shutdown)

    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)

    # A task that overruns its budget is a bug, not something to wait out: fail it
    # and let the schedule or the retry handle it.
    job_timeout = 300
    max_jobs = 10
    keep_result = 3600
    max_tries = 3
    health_check_interval = 60
