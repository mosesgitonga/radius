"""Prometheus metrics.

**The API** counts requests and times them per route *template* — `/v1/posts/{post_id}`,
never the concrete path — so the number of series is bounded by the number of
routes, not by the number of posts. Served at `/metrics`, behind
`METRICS_TOKEN`; with no token configured the endpoint does not exist, because
request counts per route are worth something to an attacker mapping the API.

**The worker** counts jobs by task and outcome, times them, and records when
each task last succeeded. That last one is what alerts are written against: a
cron that silently stops — density, reminders, retention — shows up as a
timestamp that stops moving, which no failure counter can show.

Nothing labelled here identifies a person.
"""

from __future__ import annotations

import hmac
import time
from typing import Final

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.errors import NotFoundError

UNMATCHED_ROUTE: Final = "unmatched"

HTTP_REQUESTS = Counter(
    "myradius_http_requests_total",
    "HTTP requests, by method, route template and status code.",
    ("method", "route", "status"),
)
HTTP_LATENCY = Histogram(
    "myradius_http_request_duration_seconds",
    "HTTP request latency, by method and route template.",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0),
)
JOBS = Counter(
    "myradius_worker_jobs_total", "Worker jobs, by task and outcome.", ("task", "outcome")
)
JOB_DURATION = Histogram(
    "myradius_worker_job_duration_seconds",
    "Worker job duration, by task.",
    ("task",),
    buckets=(0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 120.0, 300.0),
)
JOB_LAST_SUCCESS = Gauge(
    "myradius_worker_job_last_success_timestamp_seconds",
    "When each task last finished without error, as a Unix time.",
    ("task",),
)


def route_template(scope: Scope) -> str:
    """The matched route with its parameters put back as placeholders.

    Rebuilt from the path rather than read off the route, because a route inside an
    included router knows only its own part of the path. A request that matched no
    route is one label, whatever its path, so a scanner cannot mint series.
    """
    if scope.get("route") is None:
        return UNMATCHED_ROUTE
    names_by_value = {str(value): name for name, value in (scope.get("path_params") or {}).items()}
    segments = str(scope.get("path", "")).split("/")
    return "/".join(
        f"{{{names_by_value[segment]}}}" if segment in names_by_value else segment
        for segment in segments
    )


class MetricsMiddleware:
    """Times every HTTP request and counts it under its route template."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = route_template(scope)
            method = str(scope.get("method", "GET"))
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_LATENCY.labels(method, route).observe(time.perf_counter() - started)


def record_job(task: str, *, outcome: str, seconds: float) -> None:
    JOBS.labels(task, outcome).inc()
    JOB_DURATION.labels(task).observe(seconds)
    if outcome == "success":
        JOB_LAST_SUCCESS.labels(task).set(time.time())


router = APIRouter(include_in_schema=False)


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    expected = get_settings().metrics_token.get_secret_value()
    if not expected:
        raise NotFoundError()
    header = request.headers.get("Authorization", "")
    supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        # The same answer as no endpoint at all: a wrong token learns nothing.
        raise NotFoundError()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
