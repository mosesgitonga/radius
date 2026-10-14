"""The FCM provider's handling of what Firebase reports back.

Firebase itself is stubbed: no test reaches the network. What matters is our side —
batching, recognising dead tokens, and never raising for a delivery failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from firebase_admin import exceptions, messaging

from app.core.config import Settings
from app.core.providers import push as push_module
from app.core.providers.push import (
    FCM_MULTICAST_LIMIT,
    FcmPushProvider,
    PushMessage,
    RecordingPushProvider,
    build_push_provider,
)


@dataclass
class _Outcome:
    success: bool
    exception: Exception | None = None


@dataclass
class _Batch:
    responses: list[_Outcome]


def _provider(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FcmPushProvider:
    provider = FcmPushProvider(settings)
    monkeypatch.setattr(provider, "_get_app", lambda: object())
    return provider


async def test_dead_tokens_are_reported_and_others_counted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[Any] = []

    async def fake_send(multicast: Any, app: Any = None) -> _Batch:
        sent.append(multicast)
        return _Batch(
            [
                _Outcome(True),
                _Outcome(False, messaging.UnregisteredError("gone")),
                _Outcome(False, exceptions.UnavailableError("try later")),
            ]
        )

    monkeypatch.setattr(messaging, "send_each_for_multicast_async", fake_send)
    provider = _provider(settings, monkeypatch)

    result = await provider.send(
        tokens=["live", "dead", "flaky"],
        message=PushMessage(title="t", body="b", data={"type": "message"}, collapse_key="k"),
    )

    assert result.sent == 1
    assert result.failed == 2
    # Only the unregistered one is dead; a temporary failure is not a reason to forget it.
    assert result.invalid_tokens == frozenset({"dead"})
    assert sent[0].android.collapse_key == "k"


async def test_large_audiences_are_sent_in_batches(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_sizes: list[int] = []

    async def fake_send(multicast: Any, app: Any = None) -> _Batch:
        batch_sizes.append(len(multicast.tokens))
        return _Batch([_Outcome(True) for _ in multicast.tokens])

    monkeypatch.setattr(messaging, "send_each_for_multicast_async", fake_send)
    provider = _provider(settings, monkeypatch)
    tokens = [f"t{index}" for index in range(FCM_MULTICAST_LIMIT + 3)]

    result = await provider.send(tokens=tokens, message=PushMessage(title="t", body="b"))

    assert batch_sizes == [FCM_MULTICAST_LIMIT, 3]
    assert result.sent == len(tokens)


async def test_an_outage_does_not_raise(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_send(multicast: Any, app: Any = None) -> _Batch:
        raise exceptions.UnavailableError("FCM is down")

    monkeypatch.setattr(messaging, "send_each_for_multicast_async", failing_send)
    provider = _provider(settings, monkeypatch)

    result = await provider.send(tokens=["a", "b"], message=PushMessage(title="t", body="b"))

    assert (result.sent, result.failed, result.invalid_tokens) == (0, 2, frozenset())


async def test_the_body_is_never_logged(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_send(multicast: Any, app: Any = None) -> _Batch:
        return _Batch([_Outcome(False, exceptions.UnavailableError("x"))])

    monkeypatch.setattr(messaging, "send_each_for_multicast_async", fake_send)
    provider = _provider(settings, monkeypatch)

    await provider.send(tokens=["a"], message=PushMessage(title="t", body="secret words"))
    await RecordingPushProvider().send(
        tokens=["a"], message=PushMessage(title="t", body="secret words")
    )

    assert "secret words" not in caplog.text


def test_the_configured_provider_is_built(settings: Settings) -> None:
    assert isinstance(build_push_provider(settings), RecordingPushProvider)
    fcm = settings.model_copy(
        update={"push_provider": "fcm", "fcm_credentials_file": "/run/secrets/fcm.json"}
    )
    assert isinstance(build_push_provider(fcm), FcmPushProvider)
    assert push_module.get_push_provider() is push_module.get_push_provider()
