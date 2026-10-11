"""Application factory.

`create_app()` builds and wires the FastAPI application; `app` is the instance
Uvicorn and Gunicorn import. A factory rather than a module-level app so tests can
build an application with patched settings, and so nothing connects to anything
merely because this module was imported.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.errors import ErrorEnvelope, register_exception_handlers
from app.core.health import router as health_router
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.redis import close_redis

logger = logging.getLogger(__name__)

DESCRIPTION = """
Backend API for **MyRadius**, a hyperlocal community app for Kenya.

Everything is scoped to the caller's own **radius**, which adapts to how many
active users are nearby: it shrinks where the neighbourhood is dense and widens
where it is quiet.

**Conventions**

* All client endpoints live under `/v1`, with `snake_case` JSON.
* Lists are cursor-paginated: `{"items": [...], "next_cursor": "..." | null}`.
  Pass `next_cursor` back as the `cursor` query parameter; `null` means the end.
* Every error has the same envelope:
  `{"error": {"code": "...", "message": "...", "details": {}}}`. Switch on `code`;
  `message` is for developers, not for display.
* Timestamps are UTC, ISO 8601. Money is whole Kenyan shillings, as integers.
* Phone numbers are E.164 (`+2547…`). Input may be `07…`, `01…`, `254…`, or `+254…`.
* Every response carries `X-Request-ID`; quote it when reporting a problem.

**Privacy.** No endpoint ever returns another user's coordinates or fine-grained
location. Places are described by area label and distance bucket only.
"""

TAGS_METADATA = [
    {"name": "system", "description": "Liveness and readiness probes."},
]

# Attached to every `/v1` route so the error envelope is documented once rather
# than repeated on each endpoint.
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorEnvelope, "description": "Malformed request"},
    401: {"model": ErrorEnvelope, "description": "Missing or invalid credentials"},
    403: {"model": ErrorEnvelope, "description": "Not allowed"},
    404: {"model": ErrorEnvelope, "description": "Not found"},
    422: {"model": ErrorEnvelope, "description": "Validation failed"},
    429: {"model": ErrorEnvelope, "description": "Rate limited"},
    500: {"model": ErrorEnvelope, "description": "Unexpected server error"},
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "api_starting",
        extra={"environment": settings.app_env, "api_prefix": settings.api_prefix},
    )
    try:
        yield
    finally:
        # Release pooled sockets so a reload or a rolling restart does not leave
        # connections behind on PostgreSQL or Redis.
        await dispose_engine()
        await close_redis()
        logger.info("api_stopped")


def build_v1_router(settings: Settings) -> APIRouter:
    """The versioned client API.

    Module routers are mounted here as each phase lands them: auth and users in
    Phase 1, media and feed in Phase 2, and so on.
    """
    router = APIRouter(prefix=settings.api_prefix, responses=COMMON_ERROR_RESPONSES)
    return router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings, service="api")

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        # Interactive docs are useful in every environment here: this API is
        # consumed by a separate client team, and the schema is not a secret.
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    register_exception_handlers(app)

    # Middleware runs outermost-last: CORS is added after the request context so
    # that even a rejected preflight is logged and carries a request ID.
    app.add_middleware(RequestContextMiddleware, settings=settings)
    if origins := settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    app.include_router(health_router)
    app.include_router(build_v1_router(settings))

    return app


app = create_app()
