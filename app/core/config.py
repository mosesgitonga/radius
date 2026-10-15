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

    # -- Object storage ------------------------------------------------------
    storage_provider: Literal["s3", "memory"] = "memory"
    """`memory` keeps objects in process, for tests and a bare developer machine.
    Default so a missing credential cannot silently write to a real bucket."""

    storage_bucket: str = "myradius-media"
    storage_region: str = "auto"
    storage_endpoint_url: str = ""
    """Set for anything that is not AWS: Cloudflare R2, or MinIO in development.
    Empty means AWS S3's own endpoints."""

    storage_presign_endpoint_url: str = ""
    """The endpoint *clients* upload to, if different from the one the server uses.

    Only matters where the two networks differ. In Docker the API reaches storage
    as `http://storage:8333`, which no phone can resolve, so presigned URLs must
    be signed for `http://localhost:8333` instead — the host is part of a SigV4
    signature, so a URL cannot simply be rewritten after signing. Empty means the
    same as `storage_endpoint_url`, which is the case for R2 and S3."""

    storage_access_key_id: str = ""
    storage_secret_access_key: SecretStr = SecretStr("")
    storage_public_base_url: str = ""
    """Public base URL for serving processed images — a CDN or R2 public bucket.
    Empty falls back to presigned GETs, which work but cache badly."""

    storage_force_path_style: bool = True
    """MinIO and R2 want path-style addressing; AWS prefers virtual-hosted."""

    presign_upload_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    presign_download_ttl_seconds: int = Field(default=3600, ge=60, le=604_800)

    # -- Media ---------------------------------------------------------------
    media_max_upload_bytes: int = Field(default=12 * 1024 * 1024, ge=1024)
    """12 MB. Generous enough for a modern phone photo, small enough that the
    worker is not decoding something pathological."""

    media_max_pixels: int = Field(default=50_000_000, ge=1_000)
    """Guards against a decompression bomb: a tiny file that expands into
    gigabytes of pixels. Pillow checks this before it allocates."""

    media_webp_sizes: str = "320,720,1280"
    """Longest-edge widths, as the brief specifies. Comma-separated so the set is
    tunable without a code change."""

    media_webp_quality: int = Field(default=80, ge=1, le=100)
    media_avatar_size: int = Field(default=320, ge=64, le=2048)
    media_pending_ttl_hours: int = Field(default=24, ge=1, le=720)
    """An upload that is never completed is swept after this."""

    # -- Posts ---------------------------------------------------------------
    post_max_body_length: int = Field(default=2000, ge=1, le=20_000)
    post_max_images: int = Field(default=6, ge=1, le=20)

    post_categories: str = "general,question,recommendation,lost_and_found,alert"
    """Configurable, as the brief requires, and therefore a plain string column
    with no database enum: adding a category must not need a migration."""

    # -- Stories -------------------------------------------------------------
    story_ttl_hours: int = Field(default=24, ge=1, le=168)
    story_max_text_length: int = Field(default=280, ge=1, le=2000)

    # -- Comments and reactions ----------------------------------------------
    comment_max_length: int = Field(default=1000, ge=1, le=10_000)
    reaction_kinds: str = "like,love,helpful,celebrate,sad"
    """One reaction per user per target; changing it replaces the previous one."""

    # -- Moderation ----------------------------------------------------------
    report_auto_hide_threshold: int = Field(default=3, ge=1, le=100)
    """Distinct *trusted* reporters needed to hide content pending review. Low
    enough to act quickly on something genuinely harmful, high enough that one
    person cannot silence a neighbour they disagree with."""

    trusted_reporter_min_account_age_days: int = Field(default=7, ge=0, le=365)
    """Phase 2's stand-in for the verified badge: a reporter counts towards the
    auto-hide threshold once their account is this old, their phone is verified,
    and nothing has been upheld against them. Phase 8 replaces this with the badge."""

    # -- Rate limits for new accounts ----------------------------------------
    new_account_age_hours: int = Field(default=48, ge=0, le=8760)
    """Below this age an account is "new" and gets the tighter limits below. A
    throwaway account is the cheapest spam vector there is, and age is the one
    signal that cannot be faked without waiting."""

    new_account_posts_per_day: int = Field(default=5, ge=0)
    new_account_comments_per_hour: int = Field(default=10, ge=0)
    new_account_stories_per_day: int = Field(default=3, ge=0)

    posts_per_day: int = Field(default=30, ge=0)
    comments_per_hour: int = Field(default=60, ge=0)
    stories_per_day: int = Field(default=10, ge=0)

    # -- Idempotency ---------------------------------------------------------
    idempotency_ttl_hours: int = Field(default=24, ge=1, le=168)
    """How long a stored response is replayed for a repeated key. A day covers
    any realistic retry on a flaky mobile connection."""

    # -- Messaging -----------------------------------------------------------
    message_max_length: int = Field(default=4000, ge=1, le=20_000)
    group_chat_member_limit: int = Field(default=256, ge=2, le=5000)
    """The brief asks for a configurable limit. 256 covers an estate committee or
    a chama comfortably; larger gatherings belong in a group (Phase 4)."""

    group_chat_title_max_length: int = Field(default=80, ge=1, le=200)

    messages_per_minute: int = Field(default=30, ge=1)
    new_account_messages_per_minute: int = Field(default=10, ge=1)
    message_requests_per_day: int = Field(default=20, ge=1)
    new_account_message_requests_per_day: int = Field(default=5, ge=1)
    """A message request is the one way a stranger can reach someone, so it is the
    first thing a spammer reaches for. A new account gets a handful a day."""

    # -- Realtime ------------------------------------------------------------
    ws_auth_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    """How long a socket may stay open before authenticating. Long enough for a
    slow handshake on 3G, short enough that idle unauthenticated sockets cannot pile
    up and exhaust the server."""

    ws_heartbeat_seconds: float = Field(default=25.0, gt=0, le=300)
    """Below the 30-60 second idle timeouts common on mobile carrier NATs, so a
    connection is not silently dropped while the app sits open."""

    presence_ttl_seconds: int = Field(default=60, ge=10, le=600)
    """A connection counts as online until this long after its last heartbeat. A
    socket that dies without closing stops counting within a minute, so the user
    starts getting pushes again."""

    sync_overlap_seconds: int = Field(default=30, ge=0, le=3600)
    """`GET /v1/sync` looks this far behind the client's cursor. Conversations are
    re-listed harmlessly, because per-conversation sync by sequence is idempotent;
    without the overlap, a change committed out of timestamp order could be missed."""

    # -- Notifications -------------------------------------------------------
    push_provider: Literal["fake", "fcm"] = "fake"
    """`fake` records pushes instead of sending them. The default, so a missing
    credential cannot cause a real notification to a real phone."""

    fcm_credentials_file: str = ""
    """Path to a Firebase service-account JSON file. Never commit the file itself."""

    push_throttle_seconds: int = Field(default=60, ge=0, le=3600)
    """At most one push per collapse key in this window. Ten reactions in a minute
    become one notification, not ten buzzes."""

    notification_collapse_window_minutes: int = Field(default=60, ge=1, le=1440)
    """Repeat events with the same collapse key within this window update one
    unread in-app notification ("Kamau and 3 others reacted") instead of adding rows."""

    notification_retention_days: int = Field(default=90, ge=1, le=730)

    device_token_stale_days: int = Field(default=90, ge=7, le=365)
    """A device token not re-registered for this long is deleted. The app
    re-registers on every launch, so a token this old belongs to an uninstalled app
    or an abandoned phone."""

    # -- Groups --------------------------------------------------------------
    group_categories: str = "estate,church,chama,students,sports,business,other"
    """Configurable, as the brief requires: a plain string column, so adding a
    category is a settings change, not a migration."""

    group_name_max_length: int = Field(default=80, ge=3, le=200)
    group_description_max_length: int = Field(default=2000, ge=0, le=10_000)
    group_rules_max_length: int = Field(default=4000, ge=0, le=20_000)

    groups_per_day: int = Field(default=5, ge=1)
    new_account_groups_per_day: int = Field(default=1, ge=1)
    """Creating a group is how a scammer gets an audience-shaped thing to point
    people at, so a new account gets one a day."""

    group_join_requests_per_day: int = Field(default=30, ge=1)
    group_invites_per_day: int = Field(default=100, ge=1)
    new_account_group_invites_per_day: int = Field(default=20, ge=1)

    group_invite_link_default_days: int = Field(default=7, ge=1, le=90)
    group_invite_link_max_days: int = Field(default=30, ge=1, le=365)
    """An invite link is a bearer credential for a private group, and links get
    forwarded. Every link expires; this caps how long one can be made to last."""

    group_search_min_similarity: float = Field(default=0.5, gt=0, lt=1)
    """The `pg_trgm` *word* similarity a search needs against a group's name. Word
    similarity, so one word finds a long name containing it ("kamakwa" finds
    "Kamakwa Estate Residents"), while a typo ("kamkwa") still scores above 0.5."""

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

    @model_validator(mode="after")
    def _require_storage_credentials(self) -> Settings:
        """Fail at boot rather than at the first upload."""
        if self.storage_provider == "s3" and not (
            self.storage_access_key_id and self.storage_secret_access_key.get_secret_value()
        ):
            raise ValueError(
                "STORAGE_ACCESS_KEY_ID and STORAGE_SECRET_ACCESS_KEY are required "
                "when STORAGE_PROVIDER=s3"
            )
        return self

    @model_validator(mode="after")
    def _forbid_memory_storage_in_production(self) -> Settings:
        """The memory provider loses every image when the process restarts."""
        if self.app_env == "production" and self.storage_provider == "memory":
            raise ValueError("STORAGE_PROVIDER must not be 'memory' in production")
        return self

    @model_validator(mode="after")
    def _check_media_and_content_lists(self) -> Settings:
        if not self.media_webp_size_list:
            raise ValueError("MEDIA_WEBP_SIZES must list at least one width")
        if not self.post_category_list:
            raise ValueError("POST_CATEGORIES must list at least one category")
        if not self.reaction_kind_list:
            raise ValueError("REACTION_KINDS must list at least one kind")
        # A new account with a *looser* limit than an established one would make
        # the age check pointless, and the mistake is easy to make in an env file.
        if self.new_account_posts_per_day > self.posts_per_day:
            raise ValueError("NEW_ACCOUNT_POSTS_PER_DAY must not exceed POSTS_PER_DAY")
        if self.new_account_comments_per_hour > self.comments_per_hour:
            raise ValueError("NEW_ACCOUNT_COMMENTS_PER_HOUR must not exceed COMMENTS_PER_HOUR")
        if self.new_account_stories_per_day > self.stories_per_day:
            raise ValueError("NEW_ACCOUNT_STORIES_PER_DAY must not exceed STORIES_PER_DAY")
        if not self.group_category_list:
            raise ValueError("GROUP_CATEGORIES must list at least one category")
        if self.new_account_groups_per_day > self.groups_per_day:
            raise ValueError("NEW_ACCOUNT_GROUPS_PER_DAY must not exceed GROUPS_PER_DAY")
        if self.new_account_group_invites_per_day > self.group_invites_per_day:
            raise ValueError(
                "NEW_ACCOUNT_GROUP_INVITES_PER_DAY must not exceed GROUP_INVITES_PER_DAY"
            )
        if self.group_invite_link_default_days > self.group_invite_link_max_days:
            raise ValueError(
                "GROUP_INVITE_LINK_DEFAULT_DAYS must not exceed GROUP_INVITE_LINK_MAX_DAYS"
            )
        return self

    @model_validator(mode="after")
    def _check_messaging_limits(self) -> Settings:
        if self.new_account_messages_per_minute > self.messages_per_minute:
            raise ValueError("NEW_ACCOUNT_MESSAGES_PER_MINUTE must not exceed MESSAGES_PER_MINUTE")
        if self.new_account_message_requests_per_day > self.message_requests_per_day:
            raise ValueError(
                "NEW_ACCOUNT_MESSAGE_REQUESTS_PER_DAY must not exceed MESSAGE_REQUESTS_PER_DAY"
            )
        return self

    @model_validator(mode="after")
    def _check_push_provider(self) -> Settings:
        if self.push_provider == "fcm" and not self.fcm_credentials_file:
            raise ValueError("FCM_CREDENTIALS_FILE is required when PUSH_PROVIDER=fcm")
        if self.app_env == "production" and self.push_provider == "fake":
            raise ValueError("PUSH_PROVIDER must not be 'fake' in production")
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

    @property
    def media_webp_size_list(self) -> tuple[int, ...]:
        """WebP widths, smallest first."""
        sizes = {int(part) for part in self.media_webp_sizes.split(",") if part.strip()}
        return tuple(sorted(sizes))

    @property
    def post_category_list(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.post_categories.split(",") if part.strip())

    @property
    def group_category_list(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.group_categories.split(",") if part.strip())

    @property
    def reaction_kind_list(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.reaction_kinds.split(",") if part.strip())

    @property
    def story_ttl(self) -> timedelta:
        return timedelta(hours=self.story_ttl_hours)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton.

    Cached so config is read once per process. Tests that need different values
    patch the environment and call `get_settings.cache_clear()`.
    """
    return Settings()
