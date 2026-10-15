"""Gunicorn server hooks, loaded with `--config python:app.core.gunicorn_hooks`.

Gunicorn recycles workers (`--max-requests`). Each leaves its metrics files
behind in `PROMETHEUS_MULTIPROC_DIR`; telling the client library the process has
gone keeps its live gauges out of the totals.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import multiprocess


def child_exit(server: Any, worker: Any) -> None:
    multiprocess.mark_process_dead(worker.pid)  # type: ignore[no-untyped-call]
