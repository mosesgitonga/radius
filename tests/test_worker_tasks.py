"""Worker tasks.

Tasks are called directly with a stub context rather than through a running
worker, as the brief specifies: it keeps the tests fast and deterministic, and
there is nothing arq-specific left to verify once the schedule is registered.
"""

from __future__ import annotations

import logging

import pytest
from arq.connections import RedisSettings

from app.core.context import get_request_id
from app.workers.settings import WorkerSettings
from app.workers.tasks import heartbeat, with_job_context


async def test_heartbeat_runs_and_returns_a_timestamp() -> None:
    result = await heartbeat({"job_id": "test-job"})

    assert result.endswith("+00:00")


async def test_a_task_adopts_its_job_id_as_the_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """So work enqueued by a request can be traced through the worker's logs
    using the same field the API logs."""
    seen: list[str | None] = []

    @with_job_context
    async def record(ctx: dict[str, object]) -> None:
        seen.append(get_request_id())

    await record({"job_id": "job-42"})

    assert seen == ["job-42"]


async def test_a_task_without_a_job_id_still_gets_a_correlation_id() -> None:
    seen: list[str | None] = []

    @with_job_context
    async def record(ctx: dict[str, object]) -> None:
        seen.append(get_request_id())

    await record({})

    assert seen[0] is not None


async def test_the_correlation_id_is_cleared_afterwards() -> None:
    """A worker process handles many jobs; a leaked ID would mislabel the next."""
    await heartbeat({"job_id": "job-99"})

    assert get_request_id() is None


async def test_task_failures_are_logged_and_re_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """arq decides whether to retry, so the exception must propagate — but it
    must also be logged, since nothing else sees it."""

    @with_job_context
    async def failing(ctx: dict[str, object]) -> None:
        raise ValueError("task blew up")

    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="task blew up"):
        await failing({"job_id": "job-7"})

    assert any(record.message == "task_failed" for record in caplog.records)


async def test_task_success_is_logged_with_a_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        await heartbeat({"job_id": "job-1"})

    finished = [r for r in caplog.records if r.message == "task_finished"]
    assert finished
    assert isinstance(getattr(finished[0], "duration_ms", None), float)


def test_the_worker_registers_its_tasks() -> None:
    """A task missing from `functions` is never run, with no error anywhere."""
    assert heartbeat in WorkerSettings.functions


def test_the_worker_has_a_schedule() -> None:
    assert WorkerSettings.cron_jobs


def test_the_worker_points_at_the_configured_redis() -> None:
    assert isinstance(WorkerSettings.redis_settings, RedisSettings)


def test_tasks_have_a_timeout() -> None:
    """An unbounded task holds a worker slot forever; at launch there are few."""
    assert 0 < WorkerSettings.job_timeout <= 600
