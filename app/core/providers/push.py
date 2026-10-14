"""Push delivery.

The same shape as SMS: a protocol the notifications module depends on, a fake
that records what would have been sent, and the real vendor behind it.

**What a push says is decided before it gets here.** By the time a `PushMessage`
reaches a provider, the title and body are final, and for messages they carry the
sender's name but not the text unless the recipient opted in. The provider never
logs the body, for the same reason the SMS provider does not: it may be someone's
private message, and the redaction filter cannot recognise that.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.core.config import Settings

if TYPE_CHECKING:
    from firebase_admin import App

logger = logging.getLogger(__name__)

# FCM accepts at most 500 tokens per multicast request.
FCM_MULTICAST_LIMIT = 500


@dataclass(frozen=True, slots=True)
class PushMessage:
    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)
    """String values only — an FCM requirement. IDs and types, never content."""
    collapse_key: str | None = None
    """Lets the device replace an undelivered push with a newer one for the same
    thing, so a phone coming back online gets one notification per conversation,
    not twenty."""


@dataclass(frozen=True, slots=True)
class PushResult:
    sent: int
    failed: int
    invalid_tokens: frozenset[str] = frozenset()
    """Tokens FCM says are dead: uninstalled apps, or reset tokens. The caller
    deletes them, so the next push is not wasted on them."""


@runtime_checkable
class PushProvider(Protocol):
    name: str

    async def send(self, *, tokens: list[str], message: PushMessage) -> PushResult:
        """Deliver to every token. Must not raise for a delivery failure."""
        ...


@dataclass
class RecordingPushProvider:
    """Records pushes instead of sending them. For development and tests.

    `invalid` lets a test play the part of FCM reporting a dead token.
    """

    name: str = "fake"
    sent: list[tuple[tuple[str, ...], PushMessage]] = field(default_factory=list)
    invalid: set[str] = field(default_factory=set)

    async def send(self, *, tokens: list[str], message: PushMessage) -> PushResult:
        live = [token for token in tokens if token not in self.invalid]
        if live:
            self.sent.append((tuple(live), message))
        logger.info(
            "push_not_sent_fake_provider",
            extra={"recipients": len(live), "collapse_key": message.collapse_key},
        )
        dead = frozenset(token for token in tokens if token in self.invalid)
        return PushResult(sent=len(live), failed=len(dead), invalid_tokens=dead)

    def messages_to(self, token: str) -> list[PushMessage]:
        return [message for tokens, message in self.sent if token in tokens]

    def clear(self) -> None:
        self.sent.clear()


class FcmPushProvider:
    """Firebase Cloud Messaging, through `firebase-admin`.

    The Firebase app is initialised on first use, not at import, so a worker that
    never sends a push never reads the credentials file.
    """

    name = "fcm"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._app: App | None = None

    def _get_app(self) -> App:
        if self._app is None:
            import firebase_admin
            from firebase_admin import credentials

            credential = credentials.Certificate(self._settings.fcm_credentials_file)
            # A named app, so this cannot collide with a default app some library
            # might initialise.
            self._app = firebase_admin.initialize_app(credential, name="myradius-push")
        return self._app

    async def send(self, *, tokens: list[str], message: PushMessage) -> PushResult:
        from firebase_admin import exceptions, messaging

        sent = failed = 0
        invalid: set[str] = set()
        for start in range(0, len(tokens), FCM_MULTICAST_LIMIT):
            batch = tokens[start : start + FCM_MULTICAST_LIMIT]
            # firebase-admin 7.7 deprecates `tokens` in favour of `fids`, but FIDs
            # are Firebase Installation IDs, not the registration tokens the app's
            # SDK hands us today. Moving needs a client change too; see decision 3.x.
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", "MulticastMessage.tokens", DeprecationWarning)
                multicast = messaging.MulticastMessage(
                    tokens=batch,
                    notification=messaging.Notification(title=message.title, body=message.body),
                    data=message.data,
                    android=messaging.AndroidConfig(
                        collapse_key=message.collapse_key, priority="high"
                    ),
                    apns=messaging.APNSConfig(
                        headers=(
                            {"apns-collapse-id": message.collapse_key[:64]}
                            if message.collapse_key
                            else None
                        )
                    ),
                )
            try:
                response: Any = await messaging.send_each_for_multicast_async(
                    multicast, app=self._get_app()
                )
            except (TimeoutError, exceptions.FirebaseError, ValueError, OSError) as exc:
                # The whole batch failed — an outage or a credentials problem. Not
                # raised: the notification is still in the in-app list, and a push
                # is a nudge rather than the record.
                logger.error(
                    "push_provider_unreachable",
                    extra={"provider": self.name, "error": type(exc).__name__},
                )
                failed += len(batch)
                continue

            for token, outcome in zip(batch, response.responses, strict=True):
                if outcome.success:
                    sent += 1
                    continue
                failed += 1
                if isinstance(
                    outcome.exception,
                    (messaging.UnregisteredError, messaging.SenderIdMismatchError),
                ):
                    invalid.add(token)

        if failed:
            logger.warning(
                "push_partially_failed",
                extra={
                    "provider": self.name,
                    "sent": sent,
                    "failed": failed,
                    "invalid_tokens": len(invalid),
                },
            )
        return PushResult(sent=sent, failed=failed, invalid_tokens=frozenset(invalid))


def build_push_provider(settings: Settings) -> PushProvider:
    if settings.push_provider == "fcm":
        return FcmPushProvider(settings)
    return RecordingPushProvider()


_provider: PushProvider | None = None


def get_push_provider() -> PushProvider:
    """The process-wide provider, a singleton for the same reason as SMS: so a
    test can see what a task it triggered would have sent."""
    global _provider
    if _provider is None:
        from app.core.config import get_settings

        _provider = build_push_provider(get_settings())
    return _provider


def set_push_provider(provider: PushProvider | None) -> None:
    """Override the provider. Used by tests; `None` restores the default."""
    global _provider
    _provider = provider
