"""Settings validation.

Most of this is about the guards that stop a misconfigured deployment from
booting. A service that starts with a development signing key is worse than one
that refuses to start, because nobody finds out until tokens are forged.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import INSECURE_DEV_SECRET, RadiusFeature, Settings


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
        _settings(
            app_env="production",
            secret_key="a-real-secret-of-at-least-32-bytes-length",
            debug=True,
        )


def test_production_with_proper_configuration_boots() -> None:
    settings = _settings(
        app_env="production",
        secret_key="a-real-secret-of-at-least-32-bytes-length",
        debug=False,
        sms_provider="africastalking",
        africastalking_username="myradius",
        africastalking_api_key="real-key",
        storage_provider="s3",
        storage_access_key_id="key",
        storage_secret_access_key="secret",
        push_provider="fcm",
        fcm_credentials_file="/run/secrets/fcm.json",
    )

    assert settings.is_deployed is True


def test_production_refuses_the_fake_push_provider() -> None:
    """The fake records pushes instead of sending them, so nobody would be told
    about a message while the app was closed."""
    with pytest.raises(ValidationError, match="PUSH_PROVIDER"):
        _settings(
            app_env="production",
            secret_key="a-real-secret-of-at-least-32-bytes-length",
            sms_provider="africastalking",
            africastalking_username="u",
            africastalking_api_key="k",
            storage_provider="s3",
            storage_access_key_id="key",
            storage_secret_access_key="secret",
            push_provider="fake",
        )


def test_fcm_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="FCM_CREDENTIALS_FILE"):
        _settings(push_provider="fcm")


def test_a_new_account_message_limit_looser_than_the_established_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="NEW_ACCOUNT_MESSAGE_REQUESTS_PER_DAY"):
        _settings(new_account_message_requests_per_day=50, message_requests_per_day=20)


def test_production_refuses_in_memory_storage() -> None:
    """The memory provider loses every image when the process restarts."""
    with pytest.raises(ValidationError, match="STORAGE_PROVIDER"):
        _settings(
            app_env="production",
            secret_key="a-real-secret-of-at-least-32-bytes-length",
            sms_provider="africastalking",
            africastalking_username="u",
            africastalking_api_key="k",
            storage_provider="memory",
            push_provider="fcm",
            fcm_credentials_file="/run/secrets/fcm.json",
        )


def test_s3_storage_requires_credentials() -> None:
    """Fail at boot rather than at the first upload."""
    with pytest.raises(ValidationError, match="STORAGE_ACCESS_KEY_ID"):
        _settings(storage_provider="s3")


def test_a_new_account_limit_looser_than_the_established_one_is_refused() -> None:
    """It would make the age check pointless, and it is an easy env-file mistake."""
    with pytest.raises(ValidationError, match="NEW_ACCOUNT_POSTS_PER_DAY"):
        _settings(new_account_posts_per_day=50, posts_per_day=30)


def test_production_refuses_a_short_secret_key() -> None:
    """RFC 7518: an HMAC-SHA256 key should be at least as long as the hash output.
    PyJWT only warns about a shorter one, so the refusal happens here."""
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        _settings(
            app_env="production",
            secret_key="short",
            sms_provider="africastalking",
            africastalking_username="u",
            africastalking_api_key="k",
        )


def test_local_tolerates_a_short_secret_key() -> None:
    """Developers should not need to generate a key to run the app."""
    assert _settings(secret_key="short").app_env == "local"


def test_production_refuses_the_console_sms_provider() -> None:
    """The console provider logs codes instead of sending them, so in production
    it would mean nobody can sign up and every code sits in the log."""
    with pytest.raises(ValidationError, match="SMS_PROVIDER"):
        _settings(
            app_env="production",
            secret_key="a-real-secret-of-at-least-32-bytes-length",
            sms_provider="console",
        )


def test_africastalking_requires_credentials() -> None:
    """Fails at boot rather than at the first signup attempt."""
    with pytest.raises(ValidationError, match="AFRICASTALKING"):
        _settings(sms_provider="africastalking")


def test_a_minimum_radius_above_its_maximum_is_refused() -> None:
    """Otherwise every radius silently clamps to the minimum and returns a
    plausible-looking number that is simply wrong."""
    with pytest.raises(ValidationError, match="exceeds maximum"):
        _settings(feed_radius_min_km=20.0, feed_radius_max_km=15.0)


def test_a_filter_cap_below_the_minimum_radius_is_refused() -> None:
    with pytest.raises(ValidationError, match="below"):
        _settings(marketplace_radius_min_km=10.0, marketplace_filter_max_km=5.0)


def test_the_radius_table_matches_the_brief() -> None:
    """The product's main tuning surface, pinned so a careless edit is visible."""
    rules = _settings().radius_rules

    assert rules[RadiusFeature.feed].target_active_users == 500
    assert (rules[RadiusFeature.feed].min_km, rules[RadiusFeature.feed].max_km) == (1.0, 15.0)
    assert rules[RadiusFeature.feed].user_filter_max_km is None

    assert rules[RadiusFeature.groups].feed_multiplier == 2.0
    assert rules[RadiusFeature.marketplace].feed_multiplier == 2.0
    assert rules[RadiusFeature.services].feed_multiplier == 3.0
    assert rules[RadiusFeature.events].feed_multiplier == 2.0

    assert rules[RadiusFeature.services].max_km == 40.0
    assert rules[RadiusFeature.marketplace].user_filter_max_km == 50.0


def test_coarse_resolutions_follow_the_storage_resolution() -> None:
    """Derived rather than configured separately, so the two cannot drift."""
    assert _settings().h3_coarse_resolutions == (8, 7, 6)
    assert _settings(h3_storage_resolution=10).h3_coarse_resolutions == (9, 8, 7)


def test_the_minimum_age_cannot_be_lowered_below_18() -> None:
    """Lowering it is a legal decision, not a configuration one: under-18s bring
    the Act's parental-consent regime into scope."""
    with pytest.raises(ValidationError):
        _settings(minimum_age_years=16)


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
