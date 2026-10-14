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
from app.workers.tasks import (
    as_worker_task,
    catch_up_media,
    expire_old_stories,
    heartbeat,
    process_media,
    refresh_density,
    roll_presence,
    run_retention,
)

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

    functions: ClassVar[list[WorkerCoroutine]] = [
        as_worker_task(heartbeat),
        as_worker_task(refresh_density),
        as_worker_task(roll_presence),
        as_worker_task(run_retention),
        as_worker_task(process_media),
        as_worker_task(catch_up_media),
        as_worker_task(expire_old_stories),
    ]

    cron_jobs: ClassVar[list[CronJob]] = [
        # Density every quarter hour. The radius engine reads what this writes,
        # so the cadence here is how quickly the product notices a neighbourhood
        # filling up.
        cron(
            as_worker_task(refresh_density),
            minute={0, 15, 30, 45},
            second=0,
            # Runs on boot so a fresh deployment has density immediately rather
            # than labelling everyone "nearby" until the next quarter hour.
            run_at_startup=True,
        ),
        # Scheduled in UTC, as everything here is. 00:20 UTC is 03:20 in Nairobi:
        # past midnight local time, so "today" has definitely turned over for
        # every Kenyan user, and in the quietest part of the night.
        cron(as_worker_task(roll_presence), hour={0}, minute={20}, second=0),
        cron(as_worker_task(run_retention), hour={0}, minute={40}, second=0),
        cron(as_worker_task(heartbeat), minute={0}, second=0, run_at_startup=False),
        # Every five minutes: uploads whose processing job was lost. Often enough
        # that a dropped job means a slightly late image, not a confused user.
        cron(as_worker_task(catch_up_media), minute=set(range(0, 60, 5)), second=30),
        # Every quarter hour. Reads already filter on expiry, so this is about
        # actually deleting stories — and their images — rather than hiding them.
        cron(
            as_worker_task(expire_old_stories),
            minute={7, 22, 37, 52},
            second=0,
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
