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
from app.core.queue import close_queue
from app.core.realtime import close_hub
from app.core.redis import close_redis
from app.modules.auth.router import router as auth_router
from app.modules.feed.router import router as feed_router
from app.modules.groups.router import router as groups_router
from app.modules.location.router import router as location_router
from app.modules.marketplace.router import router as marketplace_router
from app.modules.media.router import router as media_router
from app.modules.messaging.router import router as messaging_router
from app.modules.messaging.socket import router as socket_router
from app.modules.moderation.router import router as moderation_router
from app.modules.notifications.router import router as notifications_router
from app.modules.users.router import router as users_router

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
    {
        "name": "auth",
        "description": (
            "Phone verification, tokens, and device sessions. There are no "
            "passwords: identity is a phone number proved by a one-time code."
        ),
    },
    {
        "name": "users",
        "description": ("Profiles, consent records, blocking, and data-protection rights."),
    },
    {
        "name": "media",
        "description": (
            "Direct-to-storage image uploads. Every image is re-encoded with all "
            "metadata, including GPS, removed; the original is deleted."
        ),
    },
    {
        "name": "feed",
        "description": (
            "Posts, comments, reactions and stories within the caller's adaptive radius."
        ),
    },
    {
        "name": "groups",
        "description": (
            "Public groups discoverable nearby, private invite-only groups, joining, "
            "admin tools, and group posts."
        ),
    },
    {
        "name": "marketplace",
        "description": (
            "Listings nearby, search, saved listings, chatting with sellers, and marking "
            "items sold. Listings show an approximate pickup area, never a location."
        ),
    },
    {
        "name": "messaging",
        "description": (
            "Direct and group conversations, message requests, and sync. The realtime "
            "socket at `/v1/ws` announces changes; the content is fetched from here."
        ),
    },
    {
        "name": "notifications",
        "description": "Push device registration, preferences, and the in-app list.",
    },
    {"name": "moderation", "description": "Reporting content and neighbours."},
    {
        "name": "location",
        "description": (
            "Home area, explore mode, and the adaptive radius. Coordinates go in; "
            "only area labels and distance bands come out."
        ),
    },
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
        # Sockets first: the hub holds its own Redis connection.
        await close_hub()
        await dispose_engine()
        await close_redis()
        await close_queue()
        logger.info("api_stopped")


def build_v1_router(settings: Settings) -> APIRouter:
    """The versioned client API.

    Module routers are mounted here as each phase lands them. Services arrive in
    Phase 6, events in Phase 7.
    """
    router = APIRouter(prefix=settings.api_prefix, responses=COMMON_ERROR_RESPONSES)
    router.include_router(auth_router)
    router.include_router(users_router)
    router.include_router(location_router)
    router.include_router(media_router)
    router.include_router(feed_router)
    router.include_router(groups_router)
    router.include_router(marketplace_router)
    router.include_router(messaging_router)
    router.include_router(socket_router)
    router.include_router(notifications_router)
    router.include_router(moderation_router)
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
