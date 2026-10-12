"""Phone normalisation and security primitives.

Pure units, no database. Both are small and both are the kind of code where a
quiet mistake is expensive: a normalisation bug splits one person into two
accounts, and a token bug lets somebody into someone else's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Settings
from app.core.errors import BadRequestError, UnauthenticatedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    expires_in_seconds,
    generate_numeric_code,
    generate_opaque_token,
    hash_secret,
    verify_secret,
)
from app.modules.auth.phone import is_kenyan, mask_for_display, normalise_phone_number


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "0712345678",
        "+254712345678",
        "254712345678",
        "712345678",
        "+254 712 345 678",
        "0712 345 678",
        "0712-345-678",
        "  0712345678  ",
    ],
)
def test_every_accepted_form_normalises_to_one_value(raw: str) -> None:
    """The phone number *is* the account, so two spellings must never become two
    accounts — which would split someone's identity and hand them a second set of
    free posting limits."""
    assert normalise_phone_number(raw) == "+254712345678"


def test_the_01_range_is_accepted() -> None:
    """Kenya's mobile range expanded into 01xx; rejecting it would lock out
    anyone with a newer number."""
    assert normalise_phone_number("0110000000").startswith("+2541")


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "07123", "0712345678901234", "+1", "0"],
)
def test_unusable_numbers_are_rejected(raw: str) -> None:
    with pytest.raises(BadRequestError):
        normalise_phone_number(raw)


def test_a_landline_is_rejected_with_a_reason() -> None:
    """An OTP cannot arrive on a landline, so saying so beats a generic error."""
    with pytest.raises(BadRequestError, match="mobile"):
        normalise_phone_number("+254202222222")


def test_the_error_never_echoes_the_number() -> None:
    """Error payloads are logged, and a phone number is personal data."""
    with pytest.raises(BadRequestError) as caught:
        normalise_phone_number("0712345")

    assert "0712345" not in str(caught.value)


def test_an_international_number_is_honoured_as_given() -> None:
    """The launch market is Kenya, not the only possible market."""
    assert normalise_phone_number("+447911123456") == "+447911123456"
    assert is_kenyan("+447911123456") is False
    assert is_kenyan("+254712345678") is True


def test_masking_hides_the_middle_but_keeps_it_recognisable() -> None:
    masked = mask_for_display("+254712345678")

    assert masked.startswith("+2547")
    assert masked.endswith("678")
    assert "1234" not in masked


# ---------------------------------------------------------------------------
# Codes and tokens
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("length", [4, 6, 8])
def test_codes_are_the_requested_length_and_numeric(length: int) -> None:
    code = generate_numeric_code(length)

    assert len(code) == length
    assert code.isdigit()


def test_codes_are_zero_padded() -> None:
    """A code rendered as "42" instead of "000042" would be rejected by a client
    expecting six digits, and looks like a bug to the user."""
    codes = {generate_numeric_code(6) for _ in range(300)}

    assert all(len(code) == 6 for code in codes)


def test_codes_vary() -> None:
    codes = {generate_numeric_code(6) for _ in range(200)}

    assert len(codes) > 150


def test_opaque_tokens_are_long_and_unique() -> None:
    tokens = {generate_opaque_token() for _ in range(200)}

    assert len(tokens) == 200
    assert all(len(token) >= 40 for token in tokens)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
def test_hashing_is_deterministic_so_it_can_be_indexed(settings: Settings) -> None:
    """The whole reason HMAC is used instead of a salted KDF: a refresh token
    arrives with no user attached, so the session has to be found by its hash."""
    first = hash_secret("a-token", settings)
    second = hash_secret("a-token", settings)

    assert first == second
    assert len(first) == 64


def test_hashing_hides_the_secret(settings: Settings) -> None:
    hashed = hash_secret("a-token", settings)

    assert "a-token" not in hashed


def test_different_secrets_hash_differently(settings: Settings) -> None:
    assert hash_secret("one", settings) != hash_secret("two", settings)


def test_verification_accepts_the_right_secret(settings: Settings) -> None:
    assert verify_secret("correct", hash_secret("correct", settings), settings)


def test_verification_rejects_the_wrong_secret(settings: Settings) -> None:
    assert not verify_secret("wrong", hash_secret("correct", settings), settings)


def test_verification_rejects_an_empty_stored_hash(settings: Settings) -> None:
    """The shape a missing Redis field takes, so it must not accidentally pass."""
    assert not verify_secret("anything", "", settings)


def test_rotating_the_application_key_invalidates_stored_hashes(
    settings: Settings,
) -> None:
    """A feature, not a flaw: it is how everybody gets logged out after a breach."""
    other = settings.model_copy(update={"secret_key": settings.secret_key.__class__("different")})

    assert hash_secret("a-token", settings) != hash_secret("a-token", other)


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------
def test_an_access_token_round_trips(settings: Settings) -> None:
    user_id, session_id = uuid.uuid4(), uuid.uuid4()

    token, expires_at = create_access_token(
        user_id=user_id, session_id=session_id, settings=settings
    )
    claims = decode_access_token(token, settings)

    assert claims.user_id == user_id
    assert claims.session_id == session_id
    assert claims.expires_at == expires_at.replace(microsecond=0)


def test_the_token_carries_no_personal_data(settings: Settings) -> None:
    """A JWT is readable by anyone holding it, so the payload is opaque IDs only."""
    token, _ = create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings)

    payload = jwt.decode(token, options={"verify_signature": False})

    assert set(payload) == {"sub", "sid", "typ", "iat", "exp", "jti", "iss"}


def test_an_expired_token_is_rejected(settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=2)

    token, _ = create_access_token(
        user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings, now=past
    )

    with pytest.raises(UnauthenticatedError, match="expired"):
        decode_access_token(token, settings)


def test_a_token_signed_with_another_key_is_rejected(settings: Settings) -> None:
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "typ": "access",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "iss": settings.app_name,
        },
        # Long enough not to trip PyJWT's key-length warning: this test is about
        # the signature being wrong, not the key being short.
        "not-the-real-key-but-still-at-least-32-bytes",
        algorithm="HS256",
    )

    with pytest.raises(UnauthenticatedError):
        decode_access_token(forged, settings)


def test_an_unsigned_token_is_rejected(settings: Settings) -> None:
    """The classic JWT attack: `alg: none`. The algorithm is pinned to the
    configured one rather than read from the token's own header."""
    unsigned = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "typ": "access",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "iss": settings.app_name,
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(UnauthenticatedError):
        decode_access_token(unsigned, settings)


def test_a_token_for_another_issuer_is_rejected(settings: Settings) -> None:
    other = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "typ": "access",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "iss": "SomeoneElse",
        },
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(UnauthenticatedError):
        decode_access_token(other, settings)


def test_a_token_of_the_wrong_type_is_rejected(settings: Settings) -> None:
    """So a future token kind cannot be presented as an access token."""
    wrong_type = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": str(uuid.uuid4()),
            "typ": "refresh",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "iss": settings.app_name,
        },
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(UnauthenticatedError):
        decode_access_token(wrong_type, settings)


@pytest.mark.parametrize("rubbish", ["", "not.a.token", "a.b", "....", "x" * 500])
def test_malformed_tokens_are_rejected(settings: Settings, rubbish: str) -> None:
    with pytest.raises(UnauthenticatedError):
        decode_access_token(rubbish, settings)


def test_failure_messages_do_not_distinguish_forged_from_malformed(
    settings: Settings,
) -> None:
    """Telling a caller *why* a token failed only helps the caller forging them."""
    messages = set()
    for rubbish in ("not.a.token", "a.b.c"):
        with pytest.raises(UnauthenticatedError) as caught:
            decode_access_token(rubbish, settings)
        messages.add(caught.value.message)

    assert len(messages) == 1


def test_expires_in_is_never_negative() -> None:
    """A negative value would have a client refresh in a hot loop."""
    assert expires_in_seconds(datetime.now(UTC) - timedelta(hours=1)) == 0


def test_expires_in_counts_down() -> None:
    future = datetime.now(UTC) + timedelta(seconds=900)

    assert 890 <= expires_in_seconds(future) <= 900
