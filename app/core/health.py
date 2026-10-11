"""Liveness and readiness endpoints.

The two are deliberately different. `/health` answers "is this process alive?"
and touches nothing else, so a Redis outage never causes the orchestrator to kill
and restart healthy containers. `/ready` answers "should this process be sent
traffic?" and does check its dependencies, so a container with a broken database
connection is taken out of the load balancer without being destroyed.

They sit at the root rather than under `/v1`: they are operational endpoints, not
part of the client API, and their shape should never change with an API version.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.db import check_database
from app.core.errors import ErrorEnvelope, ServiceUnavailableError
from app.core.redis import check_redis

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    environment: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "ok",
                "service": "MyRadius",
                "environment": "production",
            }
        }
    }


class DependencyChecks(BaseModel):
    database: bool = Field(description="PostgreSQL answered a trivial query.")
    redis: bool = Field(description="Redis answered PING.")


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    checks: DependencyChecks

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "ready",
                "checks": {"database": True, "redis": True},
            }
        }
    }


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 whenever the process is running. Checks no dependencies.",
)
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(service=settings.app_name, environment=settings.app_env)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness probe",
    description=(
        "Checks PostgreSQL and Redis. Returns 503 with a `service_unavailable` "
        "error and the individual check results if either is unreachable."
    ),
    responses={503: {"model": ErrorEnvelope, "description": "A dependency is unreachable"}},
)
async def ready() -> ReadyResponse:
    # Checked concurrently: a readiness probe should not take the sum of its
    # dependencies' timeouts to answer.
    database_ok, redis_ok = await asyncio.gather(check_database(), check_redis())
    checks = DependencyChecks(database=database_ok, redis=redis_ok)
    if not (database_ok and redis_ok):
        raise ServiceUnavailableError(
            "One or more dependencies are unreachable",
            details={"checks": checks.model_dump()},
        )
    return ReadyResponse(checks=checks)
