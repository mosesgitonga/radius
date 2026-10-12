"""Structured logging with redaction.

Logs are JSON, one object per line, each carrying the request ID so a single
request can be traced across the API and the worker.

Redaction is not optional and not a convention — it is a filter on the root
logger, so anything that reaches the logging module is scrubbed regardless of
which module emitted it or whether that module remembered to care. Two layers:

1. **Field names.** Values of keys that name sensitive data (phone, token, otp,
   latitude, centroid, ...) are replaced wholesale.
2. **Patterns.** Phone numbers, JWTs, bearer tokens, and coordinate pairs are
   matched inside free text, because they leak through exception messages and
   third-party library logs where we do not control the field names.

What this cannot catch is a bare six-digit OTP or a lone coordinate: no pattern
distinguishes those from a page count or a duration. Those are handled by never
passing them to the logger in the first place — the field-name layer above means
anything named sensibly is safe, so name things sensibly.

Implemented on the standard library rather than structlog, which is not in the
agreed dependency list and whose value here would be mostly ergonomic.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Final

from app.core.config import Settings
from app.core.context import get_request_id

REDACTED: Final = "[redacted]"
MAX_REDACTION_DEPTH: Final = 6

# Values under these keys are replaced entirely. Matched case-insensitively
# against the whole key and against `key.endswith(...)`, so `home_latitude` and
# `author_phone_number` are covered without listing every prefix.
SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        # Credentials and tokens
        "password",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "refresh_token",
        "refresh_token_hash",
        "api_key",
        "authorization",
        "cookie",
        "set-cookie",
        # Phone and verification
        "phone",
        "phone_number",
        "msisdn",
        "otp",
        "otp_code",
        "verification_code",
        # Location: the brief forbids raw coordinates being persisted or logged
        "lat",
        "latitude",
        "lon",
        "lng",
        "longitude",
        "coords",
        "coordinates",
        "point",
        "centroid",
        "geog",
        "geom",
        "geometry",
        "h3",
        "h3_cell",
        "h3_res9",
        "cell",
        "cell_id",
    }
)

_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Kenyan mobile numbers in any of the forms the API accepts, plus E.164 generally.
    (re.compile(r"(?:\+?254|0)\s?(?:7|1)\d{2}\s?\d{3}\s?\d{3}\b"), "[redacted:phone]"),
    (re.compile(r"\+\d{9,15}\b"), "[redacted:phone]"),
    # JWTs, and anything presented as a bearer credential.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
        "[redacted:token]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted:token]"),
    # A decimal pair with coordinate-like precision, e.g. "-0.4231, 36.9476".
    (
        re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}"),
        "[redacted:coordinates]",
    ),
)

# Attributes the logging module puts on every record; anything else came from
# `extra=` and belongs in the structured output.
_STANDARD_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    return any(lowered.endswith(f"_{candidate}") for candidate in SENSITIVE_KEYS)


def redact_text(text: str) -> str:
    """Scrub sensitive patterns from free text."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively scrub a value destined for a log line.

    Containers are walked so a sensitive key nested in a payload is still caught.
    Depth is capped: a cyclic or pathological structure must not turn a log call
    into an outage.
    """
    if _depth >= MAX_REDACTION_DEPTH:
        return "[redacted:too-deep]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and is_sensitive_key(key)
            else redact(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


class RedactionFilter(logging.Filter):
    """Scrubs every record passing through the root logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Render now so `%s`-style arguments are scrubbed too; they would
        # otherwise be interpolated after this filter has run.
        try:
            record.msg = redact_text(record.getMessage())
        except (TypeError, ValueError):  # pragma: no cover — malformed format string
            record.msg = redact_text(str(record.msg))
        record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            record.__dict__[key] = REDACTED if is_sensitive_key(key) else redact(value)
        return True


class RequestContextFilter(logging.Filter):
    """Stamps the current request ID onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


def _record_extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_") and key != "request_id"
    }


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shipping in deployed environments."""

    def __init__(self, *, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "service": self.service,
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        if extras := _record_extras(record):
            payload.update(extras)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact_text(self.formatStack(record.stack_info))

        # `default=str` keeps a stray UUID or datetime from breaking a log call.
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Readable single-line output for local development."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        request_id = getattr(record, "request_id", None)
        prefix = f"{stamp} {record.levelname:<7} {record.name}"
        if request_id:
            prefix += f" [{request_id[:8]}]"
        line = f"{prefix} {record.getMessage()}"
        if extras := _record_extras(record):
            rendered = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
            line += f" {rendered}"
        if record.exc_info:
            line += "\n" + redact_text(self.formatException(record.exc_info))
        return line


def configure_logging(settings: Settings, *, service: str = "api") -> None:
    """Install the root handler. Safe to call more than once."""
    formatter: logging.Formatter = (
        JsonFormatter(service=service) if settings.log_format == "json" else ConsoleFormatter()
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    # Order matters: stamp the request ID first, then redact, so a request ID can
    # never itself carry something sensitive through.
    handler.addFilter(RequestContextFilter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Uvicorn installs its own handlers and would double-print everything.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error", "gunicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # Our own access log carries more and is redacted; uvicorn's would be noise.
    logging.getLogger("uvicorn.access").disabled = True

    # asyncpg and SQLAlchemy are chatty at INFO and will happily echo bound
    # parameters, which is exactly what must not reach the logs.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def safe_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Make a dict safe to pass as `extra=`.

    The logging module builds a `LogRecord` by assigning `extra` straight onto the
    record's `__dict__`, and it raises `KeyError` if a key would overwrite one of
    its own attributes. `created`, `module`, `message`, `name`, `args`, `filename`,
    `process` and `thread` are all reserved, and all are names a caller might
    reasonably pick for a counter or a field.

    The raise happens inside `makeRecord`, before any filter runs, so a filter
    cannot prevent it — the keys have to be corrected on the way in. Colliding keys
    are suffixed with an underscore rather than dropped, so nothing is silently
    lost from a log line.

    Use this wherever the keys are built at runtime, from a dict or a loop. Keys
    written out literally in the source are better checked by reading them.
    """
    return {
        f"{key}_" if key in _STANDARD_RECORD_ATTRS else key: value for key, value in fields.items()
    }
