"""Error tracking: Sentry, scrubbed by the same rules as the logs.

Off unless `SENTRY_DSN` is set. When on, an event leaves the process only after:

* **the request is stripped** to its method and route — no headers (they carry
  tokens), no cookies, no query string (it can carry coordinates), no body;
* **every key and string is redacted** with the log filter's rules, so a phone
  number or a coordinate pair in an exception message is caught here as it is in
  a log line;
* **no local variables are sent**: a stack frame's locals are exactly where a
  latitude or an OTP would sit;
* **no user is attached** beyond an opaque ID, and no IP address.

Sentry is a processor of personal data for the Act; sending it less is the
cheapest way to keep that relationship simple.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from app.core.config import Settings
from app.core.logging import REDACTED, is_sensitive_key, redact_text

logger = logging.getLogger(__name__)

MAX_SCRUB_DEPTH: Final = 32
_KEPT_REQUEST_FIELDS: Final = frozenset({"method", "url"})


def scrub(value: Any, *, _depth: int = 0) -> Any:
    """Redact a Sentry event, however deeply nested."""
    if _depth >= MAX_SCRUB_DEPTH:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and is_sensitive_key(key)
            else scrub(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub(item, _depth=_depth + 1) for item in value]
    return value


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sentry's `before_send`: strip the request, drop locals, redact the rest."""
    request = event.get("request")
    if isinstance(request, dict):
        kept = {key: request[key] for key in _KEPT_REQUEST_FIELDS if key in request}
        if isinstance(kept.get("url"), str):
            kept["url"] = kept["url"].split("?", 1)[0]
        event["request"] = kept
    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {"id": user["id"]} if "id" in user else {}
    for exception in (event.get("exception") or {}).get("values") or []:
        for frame in (exception.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    for thread in (event.get("threads") or {}).get("values") or []:
        for frame in (thread.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    event.pop("server_name", None)
    scrubbed: dict[str, Any] = scrub(event)
    return scrubbed


def init_sentry(settings: Settings, *, service: str) -> bool:
    """Start Sentry if a DSN is configured. Returns whether it started."""
    dsn = settings.sentry_dsn.get_secret_value()
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.app_env,
        server_name=service,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        before_send=scrub_event,  # type: ignore[arg-type]
        before_send_transaction=scrub_event,  # type: ignore[arg-type]
    )
    sentry_sdk.set_tag("service", service)
    logger.info("sentry_enabled", extra={"service": service})
    return True
