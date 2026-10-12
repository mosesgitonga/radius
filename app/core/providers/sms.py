"""SMS delivery.

One rule runs through this module: **the message body is never logged.** The body
is where the one-time code is, and no pattern can distinguish a six-digit OTP from
a page count, so the only reliable protection is not to pass it to the logger.
Recipient numbers are safe to log because the redaction filter catches phone
numbers by pattern — but the body is not, so only its length is recorded.

The console provider is the exception that proves the rule: it prints the code on
purpose, because otherwise nobody could sign in locally. A settings validator
refuses to let it run in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

# Africa's Talking returns a per-recipient status code. These three mean the
# message was accepted; anything else is a failure worth surfacing.
ACCEPTED_STATUS_CODES: frozenset[int] = frozenset({100, 101, 102})


@dataclass(frozen=True, slots=True)
class SmsResult:
    provider: str
    accepted: bool
    message_id: str | None = None
    status: str | None = None


@runtime_checkable
class SmsProvider(Protocol):
    """What the auth module needs from an SMS vendor, and nothing more."""

    name: str

    async def send(self, *, to: str, body: str) -> SmsResult:
        """Deliver one message. Must not raise for a rejected message: return a
        result with `accepted=False` so the caller can decide what to tell the
        user."""
        ...


@dataclass
class ConsoleSmsProvider:
    """Logs messages instead of sending them, and keeps them for assertions.

    Two jobs in one class on purpose. In development it prints the code so you can
    sign in; in tests it records what was sent so a test can assert on it without
    scraping log output. Neither job ever touches the network.
    """

    name: str = "console"
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def send(self, *, to: str, body: str) -> SmsResult:
        self.sent.append((to, body))
        # The one place a code is deliberately logged, so a developer can sign
        # in locally. WARNING rather than INFO because this line contains a live
        # credential and should look wrong if it ever shows up in a deployed
        # environment; a settings validator stops it reaching production at all.
        logger.warning("sms_not_sent_console_provider", extra={"to": to, "message": body})
        return SmsResult(provider=self.name, accepted=True, status="console")

    def last_message(self) -> str | None:
        return self.sent[-1][1] if self.sent else None

    def clear(self) -> None:
        self.sent.clear()


class AfricasTalkingSmsProvider:
    """Africa's Talking, over their REST API.

    Their own SDK is synchronous and would need a thread pool to use from an async
    request path, so this calls the HTTP API directly. The API is a single
    form-encoded POST, which is less code than adapting the SDK would be.
    """

    name = "africastalking"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def send(self, *, to: str, body: str) -> SmsResult:
        payload = {
            "username": self._settings.africastalking_username,
            "to": to,
            "message": body,
        }
        if self._settings.africastalking_sender_id:
            payload["from"] = self._settings.africastalking_sender_id

        headers = {
            "apiKey": self._settings.africastalking_api_key.get_secret_value(),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            response = await self._post(payload, headers)
        except httpx.HTTPError as exc:
            # A vendor outage is not an application error: the caller turns this
            # into a "try again" for the user. Note `body` is absent from the log.
            logger.error(
                "sms_provider_unreachable",
                extra={"to": to, "provider": self.name, "error": type(exc).__name__},
            )
            return SmsResult(provider=self.name, accepted=False, status="unreachable")

        return self._interpret(response, to)

    async def _post(self, payload: dict[str, str], headers: dict[str, str]) -> httpx.Response:
        url = f"{self._settings.africastalking_base_url.rstrip('/')}/version1/messaging"
        timeout = self._settings.sms_timeout_seconds
        if self._client is not None:
            return await self._client.post(url, data=payload, headers=headers, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, data=payload, headers=headers)

    def _interpret(self, response: httpx.Response, to: str) -> SmsResult:
        if response.status_code >= 400:
            logger.error(
                "sms_provider_rejected",
                extra={"to": to, "provider": self.name, "status_code": response.status_code},
            )
            return SmsResult(provider=self.name, accepted=False, status="http_error")

        try:
            recipients = response.json()["SMSMessageData"]["Recipients"]
        except (ValueError, KeyError, TypeError):
            logger.error("sms_provider_unparsable_response", extra={"provider": self.name})
            return SmsResult(provider=self.name, accepted=False, status="unparsable")

        if not recipients:
            # Happens when the number is malformed or blacklisted: HTTP 200 with
            # an empty recipient list rather than an error.
            logger.error("sms_provider_no_recipients", extra={"to": to, "provider": self.name})
            return SmsResult(provider=self.name, accepted=False, status="no_recipients")

        first = recipients[0]
        status_code = int(first.get("statusCode", 0))
        accepted = status_code in ACCEPTED_STATUS_CODES
        if not accepted:
            logger.error(
                "sms_provider_status_not_accepted",
                extra={"to": to, "provider": self.name, "provider_status": status_code},
            )
        return SmsResult(
            provider=self.name,
            accepted=accepted,
            message_id=first.get("messageId"),
            status=str(first.get("status") or status_code),
        )


def build_sms_provider(settings: Settings) -> SmsProvider:
    """Construct the configured provider."""
    if settings.sms_provider == "africastalking":
        return AfricasTalkingSmsProvider(settings)
    return ConsoleSmsProvider()


_provider: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    """The process-wide provider.

    A singleton so that the console provider's recorded messages are visible to a
    test that triggered a send through an endpoint.
    """
    global _provider
    if _provider is None:
        from app.core.config import get_settings

        _provider = build_sms_provider(get_settings())
    return _provider


def set_sms_provider(provider: SmsProvider | None) -> None:
    """Override the provider. Used by tests; `None` restores the default."""
    global _provider
    _provider = provider
