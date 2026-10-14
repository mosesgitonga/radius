"""Enqueueing background jobs from the API.

A thin seam over arq so request code says `await enqueue("process_media", id)`
without knowing about pools, and tests can swap in a recorder that never touches
Redis.

**Enqueueing is best-effort, deliberately.** If Redis is down, the request that
triggered the job still succeeds and the work is picked up by a scheduled sweep —
media processing runs a catch-up every few minutes for exactly this reason. A
user's upload should not fail because the queue blinked, and a job that silently
never runs is prevented by the sweep rather than by refusing the request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.config import get_settings
from app.core.redis import CACHE_ERRORS

logger = logging.getLogger(__name__)


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(self, job_name: str, *args: Any, **kwargs: Any) -> bool:
        """Queue a job. Returns whether it was accepted; never raises."""
        ...


class ArqJobQueue:
    """The real queue: arq over the configured Redis."""

    def __init__(self) -> None:
        self._pool: Any | None = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            self._pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        return self._pool

    async def enqueue(self, job_name: str, *args: Any, **kwargs: Any) -> bool:
        try:
            pool = await self._get_pool()
            job = await pool.enqueue_job(job_name, *args, **kwargs)
        except CACHE_ERRORS:
            logger.warning("enqueue_failed", extra={"job": job_name})
            return False
        # arq returns None when a job with the same ID is already queued, which
        # is a successful outcome here, not a failure.
        logger.info("job_enqueued", extra={"job": job_name, "queued": job is not None})
        return True

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
        self._pool = None


@dataclass
class RecordingJobQueue:
    """Records jobs instead of running them. For tests.

    Tests then call the worker task directly, which is what the brief asks for —
    asserting that a job was queued, and separately that the job does the right
    thing, rather than standing up a worker in the middle of an HTTP test.
    """

    jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    async def enqueue(self, job_name: str, *args: Any, **kwargs: Any) -> bool:
        self.jobs.append((job_name, args, kwargs))
        return True

    def names(self) -> list[str]:
        return [name for name, _, _ in self.jobs]

    def clear(self) -> None:
        self.jobs.clear()


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = ArqJobQueue()
    return _queue


def set_queue(queue: JobQueue | None) -> None:
    """Override the queue. Used by tests; `None` restores the default."""
    global _queue
    _queue = queue


async def close_queue() -> None:
    global _queue
    if isinstance(_queue, ArqJobQueue):
        await _queue.close()
    _queue = None
