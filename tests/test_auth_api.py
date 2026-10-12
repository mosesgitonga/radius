"""The authentication endpoints, end to end over HTTP.

The refresh-rotation section is the one to read carefully: reuse detection is the
subtlest behaviour in Phase 1, and the tests state the behaviour the design chose
rather than just checking status codes.
"""

from __future__ import annotations

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.providers.sms import ConsoleSmsProvider
from app.modules.auth import service as auth_service
from app.modules.auth.models import DeviceSession, SessionRevocationReason
from app.modules.auth.phone import normalise_phone_number
from app.modules.users.models import ConsentType, User, UserStatus
from app.modules.users.service import current_consents
from tests.factories import NYERI_TOWN, make_user, phone_of, unique_phone

SIGNUP_CONSENTS = {
    "is_adult": True,
    "accept_terms": True,
    "accept_privacy_policy": True,
    "accept_location_use": True,
}


async def request_code(client: httpx.AsyncClient, phone: str) -> httpx.Response:
    return await client.post("/v1/auth/otp/request", json={"phone_number": phone})


def last_code(sms: ConsoleSmsProvider) -> str:
    """Pull the code out of the message the console provider recorded."""
    message = sms.last_message()
    assert message is not None, "no SMS was sent"
    # Punctuation stripped: the code sits mid-sentence, so the token is "776951."
    digits = [
        stripped
        for part in message.split()
        if (stripped := part.strip(".,")).isdigit() and len(stripped) == 6
    ]
    assert digits, f"no six-digit code in {message!r}"
    return digits[0]


# ---------------------------------------------------------------------------
# Requesting a code
# ---------------------------------------------------------------------------
async def test_requesting_a_code_sends_one(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    response = await request_code(client, "0712345678")

    assert response.status_code == 200
    body = response.json()
    assert body["expires_in_seconds"] == 300
    assert body["resend_after_seconds"] == 60
    assert len(sms.sent) == 1
    assert sms.sent[0][0] == "+254712345678"


async def test_the_message_contains_a_code_and_a_warning(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    await request_code(client, "0712345678")

    message = sms.last_message() or ""
    assert len(last_code(sms)) == 6
    assert "not share" in message.lower()


async def test_the_masked_number_is_returned_not_the_whole_one(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    response = await request_code(client, "0712345678")

    assert response.json()["phone_number_masked"] != "+254712345678"
    assert "1234" not in response.json()["phone_number_masked"]


async def test_any_accepted_number_format_works(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    for form in ("0712345678", "+254712345678", "254712345678"):
        sms.clear()
        response = await request_code(client, form)
        assert response.status_code in (200, 429), form


async def test_a_too_short_number_is_refused_by_the_schema(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    """Length is a schema concern, so it never reaches the phone parser."""
    response = await client.post("/v1/auth/otp/request", json={"phone_number": "abc"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert not sms.sent


async def test_an_unparseable_number_is_refused_by_the_parser(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    """Long enough to pass the schema, but not a real number."""
    response = await client.post("/v1/auth/otp/request", json={"phone_number": "0000000000"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert not sms.sent


async def test_a_validation_error_never_echoes_the_number(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    """Error payloads are logged, and a phone number is personal data."""
    response = await client.post("/v1/auth/otp/request", json={"phone_number": "0000000000"})

    assert "0000000000" not in response.text


async def test_the_resend_cooldown_is_enforced(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    """Each send costs money, so a client retrying must not be able to spend it."""
    phone = unique_phone()
    assert (await request_code(client, phone)).status_code == 200

    second = await request_code(client, phone)

    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert int(second.headers["Retry-After"]) > 0
    assert len(sms.sent) == 1


async def test_the_response_is_identical_for_registered_and_unregistered_numbers(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: ConsoleSmsProvider,
) -> None:
    """Anything that differed would turn this endpoint into a way to find out who
    is registered — which, for a neighbourhood product, is a way to find out who
    lives somewhere."""
    existing = await make_user(db_session, at=NYERI_TOWN)

    registered = await request_code(client, phone_of(existing))
    unregistered = await request_code(client, unique_phone())

    assert registered.status_code == unregistered.status_code == 200
    assert set(registered.json()) == set(unregistered.json())
    assert registered.json()["expires_in_seconds"] == unregistered.json()["expires_in_seconds"]


# ---------------------------------------------------------------------------
# Verifying a code
# ---------------------------------------------------------------------------
async def test_a_new_number_gets_a_signup_token(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    phone = unique_phone()
    await request_code(client, phone)

    response = await client.post(
        "/v1/auth/otp/verify", json={"phone_number": phone, "code": last_code(sms)}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "signup_required"
    assert body["signup_token"]
    assert body["tokens"] is None


async def test_an_existing_number_is_signed_in(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: ConsoleSmsProvider,
) -> None:
    existing = await make_user(db_session, at=NYERI_TOWN)
    await request_code(client, phone_of(existing))

    response = await client.post(
        "/v1/auth/otp/verify",
        json={"phone_number": phone_of(existing), "code": last_code(sms)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "signed_in"
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]
    assert body["tokens"]["token_type"] == "bearer"
    assert body["signup_token"] is None


async def test_a_wrong_code_is_refused_and_counted(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    phone = unique_phone()
    await request_code(client, phone)

    response = await client.post(
        "/v1/auth/otp/verify", json={"phone_number": phone, "code": "000000"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["details"]["attempts_remaining"] == 4


async def test_attempts_run_out(
    client: httpx.AsyncClient, settings: Settings, sms: ConsoleSmsProvider
) -> None:
    """Five tries at a six-digit code is a 1-in-200,000 chance of a lucky guess."""
    phone = unique_phone()
    await request_code(client, phone)

    for _ in range(settings.otp_max_attempts):
        await client.post("/v1/auth/otp/verify", json={"phone_number": phone, "code": "000000"})

    exhausted = await client.post(
        "/v1/auth/otp/verify", json={"phone_number": phone, "code": "000000"}
    )

    assert exhausted.status_code == 429


async def test_the_real_code_stops_working_once_attempts_run_out(
    client: httpx.AsyncClient, settings: Settings, sms: ConsoleSmsProvider
) -> None:
    """Otherwise the attempt limit would be no limit at all."""
    phone = unique_phone()
    await request_code(client, phone)
    code = last_code(sms)

    for _ in range(settings.otp_max_attempts + 1):
        await client.post("/v1/auth/otp/verify", json={"phone_number": phone, "code": "000000"})

    response = await client.post("/v1/auth/otp/verify", json={"phone_number": phone, "code": code})

    assert response.status_code in (401, 429)


async def test_a_code_cannot_be_used_twice(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: ConsoleSmsProvider,
) -> None:
    """Consumed on success, so a code captured in transit cannot be replayed."""
    existing = await make_user(db_session, at=NYERI_TOWN)
    await request_code(client, phone_of(existing))
    code = last_code(sms)

    first = await client.post(
        "/v1/auth/otp/verify",
        json={"phone_number": phone_of(existing), "code": code},
    )
    second = await client.post(
        "/v1/auth/otp/verify",
        json={"phone_number": phone_of(existing), "code": code},
    )

    assert first.status_code == 200
    assert second.status_code == 401


async def test_verifying_without_requesting_is_refused(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/auth/otp/verify",
        json={"phone_number": unique_phone(), "code": "123456"},
    )

    assert response.status_code == 401


async def test_requesting_again_resets_the_attempt_counter(
    client: httpx.AsyncClient,
    redis_client: Redis,
    settings: Settings,
    sms: ConsoleSmsProvider,
) -> None:
    """A user who mistyped three times should not still be two attempts from a
    lockout after asking for a fresh code."""
    phone = unique_phone()
    await request_code(client, phone)
    for _ in range(3):
        await client.post("/v1/auth/otp/verify", json={"phone_number": phone, "code": "000000"})

    # The cooldown blocks a second HTTP request, so the service is called directly
    # with the cooldown key cleared — the behaviour under test is the reset, not
    # the cooldown.
    from app.core.ratelimit import RateLimiter
    from app.core.security import hash_secret

    await redis_client.delete(
        f"otp:cooldown:{hash_secret(normalise_phone_number(phone), settings)}"
    )
    sms.clear()
    await auth_service.request_otp(
        redis_client,
        sms,
        RateLimiter(redis_client, settings),
        phone_number=normalise_phone_number(phone),
        settings=settings,
    )

    response = await client.post(
        "/v1/auth/otp/verify", json={"phone_number": phone, "code": "111111"}
    )

    assert response.json()["error"]["details"]["attempts_remaining"] == 4


# ---------------------------------------------------------------------------
# Signing up
# ---------------------------------------------------------------------------
async def complete_signup(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider, phone: str, **overrides: object
) -> httpx.Response:
    await request_code(client, phone)
    verified = await client.post(
        "/v1/auth/otp/verify", json={"phone_number": phone, "code": last_code(sms)}
    )
    payload = {
        "signup_token": verified.json()["signup_token"],
        "display_name": "New Neighbour",
        **SIGNUP_CONSENTS,
        **overrides,
    }
    return await client.post("/v1/auth/signup", json=payload)


async def test_signup_creates_an_account_and_signs_in(
    client: httpx.AsyncClient, db_session: AsyncSession, sms: ConsoleSmsProvider
) -> None:
    phone = unique_phone()

    response = await complete_signup(client, sms, phone)

    assert response.status_code == 201
    body = response.json()
    assert body["tokens"]["access_token"]

    created = (
        await db_session.execute(
            select(User).where(User.phone_number == normalise_phone_number(phone))
        )
    ).scalar_one()
    assert created.display_name == "New Neighbour"
    assert created.status == UserStatus.active
    assert created.phone_verified_at is not None


async def test_signup_records_the_adult_confirmation_as_a_timestamp(
    client: httpx.AsyncClient, db_session: AsyncSession, sms: ConsoleSmsProvider
) -> None:
    """The Act wants evidence of *when* a declaration was made; a bare `true`
    records nothing worth having."""
    phone = unique_phone()
    await complete_signup(client, sms, phone)

    created = (
        await db_session.execute(
            select(User).where(User.phone_number == normalise_phone_number(phone))
        )
    ).scalar_one()

    assert created.adult_confirmed_at is not None


async def test_signup_records_each_consent_with_its_document_version(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    sms: ConsoleSmsProvider,
) -> None:
    phone = unique_phone()
    await complete_signup(client, sms, phone)

    created = (
        await db_session.execute(
            select(User).where(User.phone_number == normalise_phone_number(phone))
        )
    ).scalar_one()
    consents = await current_consents(db_session, created.id)

    assert consents[ConsentType.terms].granted is True
    assert consents[ConsentType.terms].document_version == settings.terms_version
    assert consents[ConsentType.privacy_policy].granted is True
    assert consents[ConsentType.location_use].granted is True
    # Optional and not requested, so no record at all.
    assert ConsentType.marketing not in consents


async def test_marketing_consent_is_recorded_when_given(
    client: httpx.AsyncClient, db_session: AsyncSession, sms: ConsoleSmsProvider
) -> None:
    phone = unique_phone()
    await complete_signup(client, sms, phone, accept_marketing=True)

    created = (
        await db_session.execute(
            select(User).where(User.phone_number == normalise_phone_number(phone))
        )
    ).scalar_one()
    consents = await current_consents(db_session, created.id)

    assert consents[ConsentType.marketing].granted is True


@pytest.mark.parametrize(
    "withheld",
    ["is_adult", "accept_terms", "accept_privacy_policy", "accept_location_use"],
)
async def test_signup_requires_every_mandatory_confirmation(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider, withheld: str
) -> None:
    """Enforced by the schema, so there is no code path that creates an account
    without them."""
    response = await complete_signup(client, sms, unique_phone(), **{withheld: False})

    assert response.status_code == 422


async def test_a_signup_token_is_single_use(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    """So a replayed token cannot create a second account on one number."""
    phone = unique_phone()
    await request_code(client, phone)
    verified = await client.post(
        "/v1/auth/otp/verify", json={"phone_number": phone, "code": last_code(sms)}
    )
    token = verified.json()["signup_token"]
    payload = {"signup_token": token, "display_name": "First", **SIGNUP_CONSENTS}

    first = await client.post("/v1/auth/signup", json=payload)
    second = await client.post("/v1/auth/signup", json={**payload, "display_name": "Second"})

    assert first.status_code == 201
    assert second.status_code == 401


async def test_an_invalid_signup_token_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/signup",
        json={"signup_token": "nonsense", "display_name": "Nobody", **SIGNUP_CONSENTS},
    )

    assert response.status_code == 401


async def test_a_short_display_name_is_refused(
    client: httpx.AsyncClient, sms: ConsoleSmsProvider
) -> None:
    response = await complete_signup(client, sms, unique_phone(), display_name="A")

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Refresh rotation and reuse detection
# ---------------------------------------------------------------------------
async def test_refreshing_returns_a_new_pair(
    client: httpx.AsyncClient, tokens: auth_service.TokenPair
) -> None:
    response = await client.post("/v1/auth/refresh", json={"refresh_token": tokens.refresh_token})

    assert response.status_code == 200
    body = response.json()
    assert body["refresh_token"] != tokens.refresh_token
    assert body["access_token"] != tokens.access_token


async def test_the_rotated_token_works_and_the_old_one_does_not(
    client: httpx.AsyncClient, tokens: auth_service.TokenPair
) -> None:
    rotated = (
        await client.post("/v1/auth/refresh", json={"refresh_token": tokens.refresh_token})
    ).json()

    reuse_old = await client.post("/v1/auth/refresh", json={"refresh_token": tokens.refresh_token})

    assert reuse_old.status_code == 401
    assert "sign in again" in reuse_old.json()["error"]["message"]
    # The new one is dead too: the whole family went.
    assert (
        await client.post("/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    ).status_code == 401


async def test_reusing_a_token_revokes_the_whole_family(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tokens: auth_service.TokenPair,
) -> None:
    """The design decision, stated as a test.

    A replay of a stolen token and an honest client that lost the response are
    indistinguishable, so both revoke everything descended from that sign-in. A
    thief cannot keep using what they stole; the honest user signs in once more
    rather than being quietly shadowed by somebody holding a live token.
    """
    await client.post("/v1/auth/refresh", json={"refresh_token": tokens.refresh_token})
    await client.post("/v1/auth/refresh", json={"refresh_token": tokens.refresh_token})

    family = (
        (
            await db_session.execute(
                select(DeviceSession).where(DeviceSession.family_id.is_not(None))
            )
        )
        .scalars()
        .all()
    )

    assert family
    assert all(session.revoked_at is not None for session in family)
    assert any(
        session.revoked_reason == SessionRevocationReason.reuse_detected for session in family
    )


async def test_rotation_records_what_replaced_what(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tokens: auth_service.TokenPair,
) -> None:
    """So a reuse event can be traced through the chain afterwards."""
    await client.post("/v1/auth/refresh", json={"refresh_token": tokens.refresh_token})

    original = await db_session.get(DeviceSession, tokens.session_id)

    assert original is not None
    assert original.revoked_reason == SessionRevocationReason.rotated
    assert original.replaced_by_id is not None


async def test_rotation_keeps_the_family_id(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tokens: auth_service.TokenPair,
) -> None:
    await client.post("/v1/auth/refresh", json={"refresh_token": tokens.refresh_token})

    original = await db_session.get(DeviceSession, tokens.session_id)
    assert original is not None
    successor = await db_session.get(DeviceSession, original.replaced_by_id)

    assert successor is not None
    assert successor.family_id == original.family_id


async def test_an_unknown_refresh_token_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/refresh", json={"refresh_token": "nope"})

    assert response.status_code == 401


async def test_refresh_fails_for_a_banned_account(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    banned = await make_user(db_session, at=NYERI_TOWN, status=UserStatus.banned)
    pair = await auth_service.issue_tokens(db_session, user=banned, settings=settings)
    await db_session.flush()

    response = await client.post("/v1/auth/refresh", json={"refresh_token": pair.refresh_token})

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Sessions and logout
# ---------------------------------------------------------------------------
async def test_logging_out_kills_the_session(
    auth_client: httpx.AsyncClient, tokens: auth_service.TokenPair
) -> None:
    logout = await auth_client.post("/v1/auth/logout", json={"refresh_token": tokens.refresh_token})

    assert logout.status_code == 204
    # The access token stops working immediately, not in fifteen minutes.
    assert (await auth_client.get("/v1/me")).status_code == 401


async def test_logging_out_twice_still_succeeds(
    auth_client: httpx.AsyncClient, tokens: auth_service.TokenPair
) -> None:
    """Signing out is an outcome, not an operation: failing a retry would only
    make clients paper over it."""
    await auth_client.post("/v1/auth/logout", json={"refresh_token": tokens.refresh_token})
    second = await auth_client.post("/v1/auth/logout", json={"refresh_token": tokens.refresh_token})

    assert second.status_code == 204


async def test_logout_all_keeps_the_current_device_by_default(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    user: User,
) -> None:
    """ "Sign out my other devices" must not sign out the phone in your hand."""
    for _ in range(3):
        await auth_service.issue_tokens(db_session, user=user, settings=settings)
    await db_session.flush()

    response = await auth_client.post("/v1/auth/logout-all", json={"keep_current_session": True})

    assert response.status_code == 200
    assert response.json()["sessions_revoked"] == 3
    assert (await auth_client.get("/v1/me")).status_code == 200


async def test_logout_all_can_include_the_current_device(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.post("/v1/auth/logout-all", json={"keep_current_session": False})

    assert response.status_code == 200
    assert (await auth_client.get("/v1/me")).status_code == 401


async def test_sessions_list_marks_the_current_device(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    tokens: auth_service.TokenPair,
    user: User,
) -> None:
    await auth_service.issue_tokens(
        db_session, user=user, settings=settings, device_name="Old Phone"
    )
    await db_session.flush()

    response = await auth_client.get("/v1/auth/sessions")

    assert response.status_code == 200
    sessions = response.json()
    assert len(sessions) == 2
    current = [s for s in sessions if s["is_current"]]
    assert len(current) == 1
    assert current[0]["id"] == str(tokens.session_id)


async def test_a_session_never_carries_its_token(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.get("/v1/auth/sessions")

    rendered = response.text
    assert "refresh_token" not in rendered
    assert "access_token" not in rendered


async def test_old_sessions_are_evicted_past_the_limit(
    db_session: AsyncSession, settings: Settings, user: User
) -> None:
    """Otherwise every reinstall leaves a usable refresh token behind for a month."""
    for index in range(settings.max_sessions_per_user + 3):
        await auth_service.issue_tokens(
            db_session, user=user, settings=settings, device_name=f"Device {index}"
        )
    await db_session.flush()

    live = await auth_service.count_live_sessions(db_session, user_id=user.id)

    assert live <= settings.max_sessions_per_user


async def test_sessions_require_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/auth/sessions")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
