"""Application settings.

Everything tunable lives here and is driven by environment variables, so the same
image runs locally, in CI, and in production with nothing but env changes. Keep
`.env.example` in step with this module.

The radius table at the bottom is the product's main tuning surface. Every number
in it is a guess until there is real density data from Nyeri, which is exactly why
it lives in configuration rather than in the engine.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class RadiusFeature(StrEnum):
    """Features with their own radius.

    Each reaches a different distance, because the useful neighbourhood differs by
    purpose: you want neighbours' posts from close by, but you will travel further
    for a plumber than for gossip.
    """

    feed = "feed"
    groups = "groups"
    marketplace = "marketplace"
    services = "services"
    events = "events"


class RadiusRule(BaseModel, frozen=True):
    """How one feature's radius is derived and bounded."""

    target_active_users: int | None = None
    """Density target. Only the feed has one; the rest are multiples of the feed
    radius, so that every feature widens and narrows together as an area fills up."""

    feed_multiplier: float | None = None
    min_km: float
    max_km: float

    user_filter_max_km: float | None = None
    """How far a user may widen this feature with their own distance filter. None
    means the feature has no filter — the feed is deliberately not adjustable,
    because 'show me everything' is how a local feed stops being local."""


# Only ever acceptable outside staging and production; the validator below enforces that.

# Only ever acceptable outside staging and production; the validator below enforces that.
INSECURE_DEV_SECRET = "dev-secret-not-for-production"  # noqa: S105 — the value IS the guard

# RFC 7518 section 3.2: an HMAC-SHA256 key should be at least as long as the
# hash output. A shorter key reduces the work needed to forge a token, and
# PyJWT warns about it rather than refusing, so this is enforced here.
MIN_SECRET_KEY_BYTES = 32


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

    # -- Authentication ------------------------------------------------------
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    """Symmetric, because one service both issues and verifies its own tokens.
    Move to RS256 only if something else ever needs to verify without the key."""

    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    """15 minutes: short enough that a leaked token expires before it is much use,
    long enough that a commuter on patchy data is not refreshing constantly."""

    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)
    signup_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    max_sessions_per_user: int = Field(default=10, ge=1, le=50)
    """Beyond this the oldest session is revoked, so the trail a stolen phone
    leaves behind cannot grow without bound."""

    minimum_age_years: int = Field(default=18, ge=18)
    """18 keeps the Data Protection Act's parental-consent regime out of scope.
    Lowering it is a legal decision, not a configuration one, hence the floor."""

    terms_version: str = "2026-01-01"
    privacy_policy_version: str = "2026-01-01"
    """Consent records store the version agreed to, so a policy somebody actually
    saw can be told apart from one published after they signed up."""

    # -- One-time passwords --------------------------------------------------
    otp_length: int = Field(default=6, ge=4, le=8)
    otp_ttl_seconds: int = Field(default=300, ge=60, le=900)
    otp_max_attempts: int = Field(default=5, ge=1, le=10)
    otp_resend_cooldown_seconds: int = Field(default=60, ge=10, le=600)
    otp_daily_cap_per_phone: int = Field(default=5, ge=1, le=50)

    otp_daily_cap_per_ip: int = Field(default=20, ge=1, le=500)
    """Per-IP caps stay generous on purpose: carrier NAT means a whole estate can
    share one address, and a tight cap would lock all of them out together."""

    # -- SMS -----------------------------------------------------------------
    sms_provider: Literal["console", "africastalking"] = "console"
    """`console` logs the code instead of sending it. It is the default so that a
    missing credential cannot cause real SMS spend by accident."""

    africastalking_username: str = ""
    africastalking_api_key: SecretStr = SecretStr("")
    africastalking_sender_id: str = ""
    africastalking_base_url: str = "https://api.africastalking.com"
    sms_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # -- Location and H3 -----------------------------------------------------
    h3_storage_resolution: int = Field(default=9, ge=7, le=12)
    """Resolution 9 spans a few hundred metres: fine enough to be useful, coarse
    enough that a cell is a neighbourhood rather than a house."""

    h3_radius_resolution: int = Field(default=8, ge=6, le=10)
    """Radii are computed per res-8 cell so a result can be cached and shared by
    everyone in that cell instead of recomputed per user."""

    k_anonymity_min_users: int = Field(default=10, ge=2, le=1000)
    """An area label is only shown at a cell size holding at least this many
    registered users. Below it, the label snaps up to a coarser cell."""

    home_change_cooldown_days: int = Field(default=14, ge=0, le=365)

    explore_ttl_seconds: int = Field(default=7200, ge=300, le=86_400)
    """Explore mode lives in Redis only, for about two hours, and never reaches
    PostgreSQL. See decision 1.1."""

    location_spoof_max_speed_kmh: float = Field(default=900.0, gt=0)
    """Above this a jump is implausible for ground travel and the update is
    flagged. Set at airliner speed deliberately: people do fly between Kenyan
    towns, and a false positive locks someone out of the product's core feature."""

    # -- Radius engine -------------------------------------------------------
    active_user_window_days: int = Field(default=14, ge=1, le=365)
    """A user counts as active if the app was opened within this window."""

    cell_activity_refresh_minutes: int = Field(default=15, ge=1, le=1440)
    radius_cache_ttl_seconds: int = Field(default=900, ge=30, le=86_400)

    radius_hysteresis_ratio: float = Field(default=0.20, ge=0.0, le=1.0)
    """A cached radius is replaced only when the new value differs by more than
    this. Without it the radius drifts on every refresh and the feed visibly
    reshuffles, which reads to a user as a bug."""

    radius_max_ring_k: int = Field(default=40, ge=1, le=200)
    """Safety bound on ring expansion. Each res-8 ring is roughly a kilometre, so
    this covers the largest configured maximum with room spare, and stops a
    sparse-data bug becoming an unbounded loop."""

    radius_display_rounding_km: float = Field(default=0.5, gt=0, le=5)
    """`GET /v1/me/radius` rounds to this, so the app can show "3 km" rather than a
    precise figure that would hint at how dense the caller's own street is."""

    # Feed is the only feature with a density target; the rest are multiples of it,
    # so every feature widens and narrows together as an area fills up.
    feed_target_active_users: int = Field(default=500, ge=1)
    feed_radius_min_km: float = Field(default=1.0, gt=0)
    feed_radius_max_km: float = Field(default=15.0, gt=0)

    groups_radius_multiplier: float = Field(default=2.0, gt=0)
    groups_radius_min_km: float = Field(default=2.0, gt=0)
    groups_radius_max_km: float = Field(default=25.0, gt=0)
    groups_filter_max_km: float = Field(default=25.0, gt=0)

    marketplace_radius_multiplier: float = Field(default=2.0, gt=0)
    marketplace_radius_min_km: float = Field(default=2.0, gt=0)
    marketplace_radius_max_km: float = Field(default=30.0, gt=0)
    marketplace_filter_max_km: float = Field(default=50.0, gt=0)

    services_radius_multiplier: float = Field(default=3.0, gt=0)
    services_radius_min_km: float = Field(default=3.0, gt=0)
    services_radius_max_km: float = Field(default=40.0, gt=0)
    services_filter_max_km: float = Field(default=50.0, gt=0)

    events_radius_multiplier: float = Field(default=2.0, gt=0)
    events_radius_min_km: float = Field(default=2.0, gt=0)
    events_radius_max_km: float = Field(default=30.0, gt=0)
    events_filter_max_km: float = Field(default=50.0, gt=0)

    # -- Data rights ---------------------------------------------------------
    deletion_grace_period_days: int = Field(default=30, ge=0, le=365)
    """A deletion request stays reversible for this long, then is purged. Guards
    against an account being destroyed in a moment of anger, or by someone else."""

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
            secret = self.secret_key.get_secret_value()
            if secret == INSECURE_DEV_SECRET:
                raise ValueError(
                    "SECRET_KEY must be set to a unique random value outside local and test"
                )
            if len(secret.encode()) < MIN_SECRET_KEY_BYTES:
                raise ValueError(
                    f"SECRET_KEY must be at least {MIN_SECRET_KEY_BYTES} bytes; "
                    "generate one with "
                    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
                )
            if self.debug:
                raise ValueError("DEBUG must be false in staging and production")
        return self

    @model_validator(mode="after")
    def _check_radius_bounds(self) -> Settings:
        """A minimum above its maximum would clamp every radius to the minimum and
        produce a plausible-looking number that is simply wrong."""
        for feature, rule in self.radius_rules.items():
            if rule.min_km > rule.max_km:
                raise ValueError(
                    f"{feature}: minimum radius {rule.min_km} km exceeds maximum {rule.max_km} km"
                )
            if rule.user_filter_max_km is not None and rule.user_filter_max_km < rule.min_km:
                raise ValueError(
                    f"{feature}: user filter cap {rule.user_filter_max_km} km is below "
                    f"the minimum radius {rule.min_km} km"
                )
        return self

    @model_validator(mode="after")
    def _require_sms_credentials(self) -> Settings:
        """Fail at boot rather than at the first signup attempt."""
        if self.sms_provider == "africastalking" and not (
            self.africastalking_username and self.africastalking_api_key.get_secret_value()
        ):
            raise ValueError(
                "AFRICASTALKING_USERNAME and AFRICASTALKING_API_KEY are required "
                "when SMS_PROVIDER=africastalking"
            )
        return self

    @model_validator(mode="after")
    def _forbid_console_sms_in_production(self) -> Settings:
        """The console provider logs codes instead of sending them, so in production
        it would mean nobody can sign up and every code sits in the log."""
        if self.app_env == "production" and self.sms_provider == "console":
            raise ValueError("SMS_PROVIDER must not be 'console' in production")
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

    @property
    def radius_rules(self) -> dict[RadiusFeature, RadiusRule]:
        """The radius table, assembled from the flat settings above.

        Flat scalar settings rather than one nested object, because every number
        here gets tuned individually against real density data, and one
        environment variable per number is what makes that possible without a
        code change.
        """
        return {
            RadiusFeature.feed: RadiusRule(
                target_active_users=self.feed_target_active_users,
                min_km=self.feed_radius_min_km,
                max_km=self.feed_radius_max_km,
                user_filter_max_km=None,
            ),
            RadiusFeature.groups: RadiusRule(
                feed_multiplier=self.groups_radius_multiplier,
                min_km=self.groups_radius_min_km,
                max_km=self.groups_radius_max_km,
                user_filter_max_km=self.groups_filter_max_km,
            ),
            RadiusFeature.marketplace: RadiusRule(
                feed_multiplier=self.marketplace_radius_multiplier,
                min_km=self.marketplace_radius_min_km,
                max_km=self.marketplace_radius_max_km,
                user_filter_max_km=self.marketplace_filter_max_km,
            ),
            RadiusFeature.services: RadiusRule(
                feed_multiplier=self.services_radius_multiplier,
                min_km=self.services_radius_min_km,
                max_km=self.services_radius_max_km,
                user_filter_max_km=self.services_filter_max_km,
            ),
            RadiusFeature.events: RadiusRule(
                feed_multiplier=self.events_radius_multiplier,
                min_km=self.events_radius_min_km,
                max_km=self.events_radius_max_km,
                user_filter_max_km=self.events_filter_max_km,
            ),
        }

    @property
    def h3_coarse_resolutions(self) -> tuple[int, ...]:
        """Resolutions an area label may snap up through, finest first.

        Derived from the storage resolution so the two cannot drift apart: res-9
        storage gives 8, 7, 6 — roughly a neighbourhood, a suburb, then a town.
        """
        finest = self.h3_storage_resolution - 1
        return tuple(range(finest, max(finest - 3, 0), -1))

    @property
    def access_token_ttl(self) -> timedelta:
        return timedelta(seconds=self.access_token_ttl_seconds)

    @property
    def refresh_token_ttl(self) -> timedelta:
        return timedelta(days=self.refresh_token_ttl_days)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton.

    Cached so config is read once per process. Tests that need different values
    patch the environment and call `get_settings.cache_clear()`.
    """
    return Settings()
