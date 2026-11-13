"""`GET /v1/app/version` (decision M.12)."""

from __future__ import annotations

import httpx

from app.core.config import Settings, get_settings
from app.main import create_app


async def _ask(settings: Settings, path: str = "/v1/app/version") -> httpx.Response:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def test_unset_means_nobody_is_told_to_update(settings: Settings) -> None:
    quiet = settings.model_copy(
        update={
            "app_latest_build": None,
            "app_min_supported_build": None,
            "app_update_url_android": "",
        }
    )

    response = await _ask(quiet)

    assert response.status_code == 200
    assert response.json() == {
        "latest_build": None,
        "min_supported_build": None,
        "update_url": None,
        "message": None,
    }


async def test_builds_and_the_platform_s_link_without_signing_in(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "app_latest_build": 7,
            "app_min_supported_build": 3,
            "app_update_url_android": "https://example.invalid/myradius.apk",
            "app_update_url_ios": "https://example.invalid/ios",
            "app_update_message": "New: groups.",
        }
    )

    android = await _ask(configured)
    ios = await _ask(configured, "/v1/app/version?platform=ios")

    assert android.json() == {
        "latest_build": 7,
        "min_supported_build": 3,
        "update_url": "https://example.invalid/myradius.apk",
        "message": "New: groups.",
    }
    assert ios.json()["update_url"] == "https://example.invalid/ios"


async def test_an_unknown_platform_is_refused(settings: Settings) -> None:
    response = await _ask(settings, "/v1/app/version?platform=symbian")

    assert response.status_code == 422
