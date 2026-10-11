"""Settings validation.

Most of this is about the guards that stop a misconfigured deployment from
booting. A service that starts with a development signing key is worse than one
that refuses to start, because nobody finds out until tokens are forged.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import INSECURE_DEV_SECRET, Settings


def _settings(**overrides: object) -> Settings:
    """Build settings from explicit values, ignoring the ambient environment."""
    defaults: dict[str, object] = {
        "app_env": "local",
        "secret_key": "test-secret",
        "database_url": "postgresql+asyncpg://user:pw@localhost:5432/db",
        "redis_url": "redis://localhost:6379/0",
    }
    return Settings(_env_file=None, **(defaults | overrides))  # type: ignore[arg-type]


def test_defaults_are_usable_locally() -> None:
    settings = _settings()

    assert settings.app_env == "local"
    assert settings.api_prefix == "/v1"
    assert settings.rate_limit_enabled is True


def test_production_refuses_the_development_secret() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        _settings(app_env="production", secret_key=INSECURE_DEV_SECRET)


def test_staging_refuses_the_development_secret() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        _settings(app_env="staging", secret_key=INSECURE_DEV_SECRET)


def test_production_refuses_debug_mode() -> None:
    with pytest.raises(ValidationError, match="DEBUG"):
        _settings(app_env="production", secret_key="a-real-secret", debug=True)


def test_production_with_proper_configuration_boots() -> None:
    settings = _settings(app_env="production", secret_key="a-real-secret", debug=False)

    assert settings.is_deployed is True


def test_local_tolerates_the_development_secret() -> None:
    """Otherwise every developer needs a secret before anything runs."""
    assert _settings(secret_key=INSECURE_DEV_SECRET).app_env == "local"


def test_a_synchronous_database_url_is_rejected() -> None:
    """Caught at boot rather than at the first query: the sync driver would work
    for migrations and then fail on every request."""
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(database_url="postgresql://user:pw@localhost:5432/db")


def test_the_alembic_url_drops_the_async_driver() -> None:
    settings = _settings(database_url="postgresql+asyncpg://user:pw@host:5432/db")

    assert settings.sync_database_url == "postgresql://user:pw@host:5432/db"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("https://admin.myradius.co.ke", ["https://admin.myradius.co.ke"]),
        ("https://a.example, https://b.example", ["https://a.example", "https://b.example"]),
        ("  ,  ", []),
    ],
)
def test_cors_origins_parse_from_a_comma_separated_string(raw: str, expected: list[str]) -> None:
    """A plain string, not JSON: pydantic-settings expects JSON for list-typed
    fields, which is a trap for whoever writes the `.env`."""
    assert _settings(cors_origins=raw).cors_origin_list == expected


@pytest.mark.parametrize("raw", ["v1", "/v1", "/v1/", "v1/"])
def test_api_prefix_is_normalised(raw: str) -> None:
    assert _settings(api_prefix=raw).api_prefix == "/v1"


def test_access_log_exclusions_parse() -> None:
    settings = _settings(log_access_exclude="/health, /ready")

    assert settings.access_log_excluded_paths == frozenset({"/health", "/ready"})


def test_the_secret_key_is_not_printed_in_a_repr() -> None:
    """`SecretStr` so an accidental log or traceback of settings cannot leak it."""
    settings = _settings(secret_key="super-secret-value")

    assert "super-secret-value" not in repr(settings)
    assert settings.secret_key.get_secret_value() == "super-secret-value"


def test_pool_sizes_are_bounded() -> None:
    """A pool larger than PostgreSQL's max_connections fails at peak load, which
    is the worst time to discover it."""
    with pytest.raises(ValidationError):
        _settings(db_pool_size=0)
    with pytest.raises(ValidationError):
        _settings(db_pool_size=1000)


def test_unknown_environment_variables_are_ignored_not_fatal() -> None:
    """The container's environment carries plenty that is not ours."""
    settings = Settings(_env_file=None, secret_key="x", some_unrelated_var="y")

    assert settings.secret_key.get_secret_value() == "x"
