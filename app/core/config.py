"""Application settings.

Everything tunable lives here and is driven by environment variables, so the same
image runs locally, in CI, and in production with nothing but env changes. Keep
`.env.example` in step with this module.

Phase 0 covers app, database, Redis, logging, rate limiting, CORS, and pagination.
Location and radius settings arrive in Phase 1 alongside the engine that reads them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]

# Only ever acceptable outside staging and production; the validator below enforces that.
INSECURE_DEV_SECRET = "dev-secret-not-for-production"  # noqa: S105 — the value IS the guard


class Settings(BaseSettings):
    """Typed application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application ---------------------------------------------------------
    app_name: str = "MyRadius"
    app_env: Environment = "local"
    debug: bool = False
    api_prefix: str = "/v1"

    secret_key: SecretStr = SecretStr(INSECURE_DEV_SECRET)
    """Signing key for tokens and other server-side secrets. Used from Phase 1."""

    # -- Database ------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://myradius:myradius@localhost:5432/myradius"
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout: float = Field(default=30.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, gt=0)
    db_echo: bool = False

    db_statement_timeout_ms: int = Field(default=10_000, ge=0)
    """Server-side statement timeout; 0 disables it. Guards against one slow
    geospatial query pinning a connection for the whole request budget."""

    # -- Redis ---------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = Field(default=50, ge=1)

    # -- Logging -------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    log_access: bool = True
    """Emit one structured line per request. Paths in `log_access_exclude` are skipped."""

    log_access_exclude: str = "/health,/ready,/metrics"

    # -- HTTP ----------------------------------------------------------------
    cors_origins: str = ""
    """Comma-separated allowed origins. Empty disables CORS, which is the right
    default: the Flutter clients are native and send no Origin header."""

    trust_forwarded_for: bool = False
    """Enable only when the API sits behind a reverse proxy that overwrites
    `X-Forwarded-For`. Trusting it otherwise lets clients forge their own IP and
    walk straight through per-IP rate limits."""

    # -- Rate limiting -------------------------------------------------------
    rate_limit_enabled: bool = True

    rate_limit_fail_open: bool = True
    """When Redis is unreachable: allow requests through (true) or reject them
    (false). Default true so a Redis blip does not take the API down; endpoints
    where abuse costs more than downtime (OTP, payments) opt out individually."""

    # -- Pagination ----------------------------------------------------------
    pagination_default_limit: int = Field(default=20, ge=1, le=200)
    pagination_max_limit: int = Field(default=100, ge=1, le=500)

    # -- Validation ----------------------------------------------------------
    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """The app talks to PostgreSQL over asyncpg. A sync URL would fail at
        request time rather than at boot, which is a much worse place to find out.
        Alembic gets a blocking URL from `sync_database_url` instead.
        """
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// scheme")
        return value

    @field_validator("api_prefix")
    @classmethod
    def _normalise_prefix(cls, value: str) -> str:
        return "/" + value.strip("/")

    @model_validator(mode="after")
    def _guard_deployed_environments(self) -> Settings:
        if self.app_env in ("staging", "production"):
            if self.secret_key.get_secret_value() == INSECURE_DEV_SECRET:
                raise ValueError(
                    "SECRET_KEY must be set to a unique random value outside local and test"
                )
            if self.debug:
                raise ValueError("DEBUG must be false in staging and production")
        return self

    # -- Derived -------------------------------------------------------------
    @property
    def is_deployed(self) -> bool:
        return self.app_env in ("staging", "production")

    @property
    def sync_database_url(self) -> str:
        """Blocking URL for tooling that cannot speak asyncpg, such as Alembic's
        offline mode."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def access_log_excluded_paths(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.log_access_exclude.split(",") if p.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton.

    Cached so config is read once per process. Tests that need different values
    patch the environment and call `get_settings.cache_clear()`.
    """
    return Settings()
