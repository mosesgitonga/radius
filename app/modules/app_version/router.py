"""`GET /v1/app/version`: the latest build, the oldest still supported, and
where to get a new one.

No sign-in, so a guest and a build too old to sign in both get an answer. The
request names only a platform, and nothing about it is kept.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.ratelimit import rate_limit

router = APIRouter(prefix="/app", tags=["app"])


class AppVersionOut(BaseModel):
    latest_build: int | None = Field(
        description="The newest build. Older ones may offer an update. Null: no update to offer."
    )
    min_supported_build: int | None = Field(
        description="Older builds must update before going on. Null: every build is supported."
    )
    update_url: str | None = Field(description="Where to get the new build, for this platform.")
    message: str | None = Field(
        default=None, description="An optional line to show with the update."
    )


@router.get(
    "/version",
    response_model=AppVersionOut,
    summary="Is there a newer app build?",
    description=(
        "Compares nothing itself: the app sends no build number and decides for "
        "itself. Builds older than `min_supported_build` must update; older than "
        "`latest_build` may. Changes delivered over the air don't change the build "
        "number, so they never show here."
    ),
    dependencies=[
        Depends(rate_limit(bucket="app_version", limit=120, window_seconds=3600, scope="ip"))
    ],
)
async def app_version(
    settings: Annotated[Settings, Depends(get_settings)],
    platform: Annotated[Literal["android", "ios"], Query()] = "android",
) -> AppVersionOut:
    url = settings.app_update_url_android if platform == "android" else settings.app_update_url_ios
    return AppVersionOut(
        latest_build=settings.app_latest_build,
        min_supported_build=settings.app_min_supported_build,
        update_url=url or None,
        message=settings.app_update_message or None,
    )
