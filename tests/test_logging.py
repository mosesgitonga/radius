"""Log redaction.

This is a privacy test, not a formatting test. The brief requires that phone
numbers, OTPs, tokens, and coordinates never reach the logs, and redaction is
enforced as a filter on the root logger precisely so that no individual module
has to remember. These tests hold that guarantee in place.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.config import Settings
from app.core.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    RequestContextFilter,
    configure_logging,
    is_sensitive_key,
    redact,
    redact_text,
)


@pytest.mark.parametrize(
    "raw",
    [
        "+254712345678",
        "0712345678",
        "254712345678",
        "+254112345678",
        "0112345678",
        "+254 712 345 678",
    ],
)
def test_phone_numbers_are_redacted_in_text(raw: str) -> None:
    """Every input form the API accepts must be caught, not just E.164."""
    result = redact_text(f"sending OTP to {raw} now")

    assert raw not in result
    assert "[redacted:phone]" in result


def test_jwts_are_redacted_in_text() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"

    result = redact_text(f"token={token}")

    assert token not in result
    assert "[redacted:token]" in result


def test_bearer_credentials_are_redacted_in_text() -> None:
    result = redact_text("Authorization: Bearer abc123def456ghi789")

    assert "abc123def456ghi789" not in result
    assert "[redacted:token]" in result


def test_coordinate_pairs_are_redacted_in_text() -> None:
    """Coordinates must not reach the logs even inside a third-party message."""
    result = redact_text("ValueError: bad point -0.42013, 36.94761 rejected")

    assert "-0.42013" not in result
    assert "36.94761" not in result
    assert "[redacted:coordinates]" in result


def test_ordinary_numbers_survive() -> None:
    """Redaction that eats legitimate values makes logs useless and gets disabled."""
    message = "feed query took 42.5 ms and returned 20 of 1500 posts"

    assert redact_text(message) == message


@pytest.mark.parametrize(
    "key",
    [
        "phone",
        "phone_number",
        "PHONE_NUMBER",
        "otp",
        "verification_code",
        "access_token",
        "latitude",
        "longitude",
        "centroid",
        "h3_cell",
        "author_phone",
        "home_latitude",
        "user_refresh_token",
    ],
)
def test_sensitive_keys_are_recognised(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["post_id", "status_code", "duration_ms", "area_label"])
def test_ordinary_keys_are_not_sensitive(key: str) -> None:
    assert not is_sensitive_key(key)


def test_sensitive_values_are_replaced_wholesale() -> None:
    scrubbed = redact({"user_id": "abc", "latitude": -0.42, "phone": "+254712345678"})

    assert scrubbed == {"user_id": "abc", "latitude": REDACTED, "phone": REDACTED}


def test_redaction_reaches_nested_structures() -> None:
    """Sensitive data arrives inside payloads, not only as top-level fields."""
    scrubbed = redact({"request": {"body": {"location": {"longitude": 36.94, "accuracy": 12}}}})

    nested = scrubbed["request"]["body"]["location"]
    assert nested == {"longitude": REDACTED, "accuracy": 12}


def test_redaction_walks_lists() -> None:
    scrubbed = redact({"users": [{"phone": "+254712345678"}, {"phone": "+254712345679"}]})

    assert scrubbed["users"] == [{"phone": REDACTED}, {"phone": REDACTED}]


def test_deeply_nested_structures_are_cut_off_rather_than_recursing() -> None:
    """A pathological payload must not turn a log call into an outage."""
    deep: dict[str, object] = {"leaf": "value"}
    for _ in range(50):
        deep = {"nested": deep}

    scrubbed = redact(deep)

    rendered = json.dumps(scrubbed)
    assert "too-deep" in rendered


def test_filter_redacts_the_formatted_message() -> None:
    """`%s`-style arguments are interpolated after filtering, so they are
    rendered early and scrubbed too."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="otp sent to %s",
        args=("+254712345678",),
        exc_info=None,
    )

    RedactionFilter().filter(record)

    assert "+254712345678" not in record.getMessage()
    assert "[redacted:phone]" in record.getMessage()


def test_filter_redacts_structured_extras() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="location_updated",
        args=(),
        exc_info=None,
    )
    record.__dict__["latitude"] = -0.4201
    record.__dict__["user_id"] = "97c1f3f2"

    RedactionFilter().filter(record)

    assert record.__dict__["latitude"] == REDACTED
    assert record.__dict__["user_id"] == "97c1f3f2"


def test_json_formatter_emits_one_parsable_object() -> None:
    record = logging.LogRecord(
        name="app.feed",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="post_created",
        args=(),
        exc_info=None,
    )
    record.__dict__["post_id"] = "42"

    rendered = JsonFormatter(service="api").format(record)

    payload = json.loads(rendered)
    assert payload["event"] == "post_created"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.feed"
    assert payload["service"] == "api"
    assert payload["post_id"] == "42"
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_redacts_exception_tracebacks() -> None:
    """Exception text is a common accidental leak: it quotes the bad input."""
    try:
        raise ValueError("invalid point -0.42013, 36.94761")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    rendered = JsonFormatter(service="api").format(record)

    assert "36.94761" not in rendered
    assert "[redacted:coordinates]" in rendered


def test_request_context_filter_adds_the_request_id() -> None:
    from app.core.context import reset_request_id, set_request_id

    token = set_request_id("abc123")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        RequestContextFilter().filter(record)
        assert record.__dict__["request_id"] == "abc123"
    finally:
        reset_request_id(token)


def test_configure_logging_is_idempotent(settings: Settings) -> None:
    """Called by both the app factory and the worker startup hook; calling it
    twice must not double every log line."""
    configure_logging(settings)
    configure_logging(settings)

    assert len(logging.getLogger().handlers) == 1


def test_configure_logging_installs_both_filters(settings: Settings) -> None:
    configure_logging(settings)

    handler = logging.getLogger().handlers[0]
    installed = {type(f).__name__ for f in handler.filters}
    assert installed == {"RequestContextFilter", "RedactionFilter"}
