"""Security primitives: secrets, hashing, and access tokens.

There are no passwords in this product. Identity is a phone number proved by a
one-time code, after which the client holds a short-lived access token and a
long-lived refresh token. This module makes and checks those three things, and
nothing here knows what a user is — the auth module composes it.

**Why HMAC-SHA256 and not bcrypt or Argon2.** The usual advice is a deliberately
slow KDF, which exists to make guessing a *human-chosen* password expensive. The
secrets here are not human-chosen: refresh tokens are 32 random bytes and OTPs are
rate-limited to five attempts inside five minutes, so there is nothing to guess.
What is needed instead is a hash that can be *looked up* — a refresh token arrives
with no user attached, so the server has to find the session by its hash, and a
salted KDF produces a different output every time and cannot be indexed. HMAC
keyed with the application secret gives a deterministic, indexable digest that is
still useless to anyone who steals the database without also stealing the key.

OTPs get the same treatment, so a Redis snapshot does not hand over live codes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import jwt
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import UnauthenticatedError

TOKEN_TYPE_ACCESS: Final = "access"  # noqa: S105 — a token-type tag, not a secret

# 32 bytes of entropy. Long enough that guessing is not a threat model, short
# enough to sit comfortably in a mobile client's secure storage.
REFRESH_TOKEN_BYTES: Final = 32
OPAQUE_TOKEN_BYTES: Final = 32


class AccessTokenClaims(BaseModel):
    """The verified contents of an access token."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    token_type: Literal["access"]
    expires_at: datetime


# ---------------------------------------------------------------------------
# Random secrets
# ---------------------------------------------------------------------------
def generate_numeric_code(length: int) -> str:
    """A uniformly random numeric code, zero-padded to `length`.

    `secrets.randbelow` over the whole range rather than digit-by-digit choice:
    both are fine here, but one call makes the uniformity obvious to a reader.
    """
    upper = 10**length
    return str(secrets.randbelow(upper)).zfill(length)


def generate_opaque_token(num_bytes: int = OPAQUE_TOKEN_BYTES) -> str:
    """A URL-safe random token, for refresh tokens and signup tokens."""
    return secrets.token_urlsafe(num_bytes)


# ---------------------------------------------------------------------------
# Keyed hashing
# ---------------------------------------------------------------------------
def hash_secret(value: str, settings: Settings) -> str:
    """Keyed digest of a high-entropy secret, safe to store and to index.

    Keyed with the application secret, so a stolen database or Redis snapshot
    yields nothing without the key. Rotating `SECRET_KEY` therefore invalidates
    every stored refresh token and pending OTP, which is a feature: it is how you
    log everybody out after a breach.
    """
    return hmac.new(
        settings.secret_key.get_secret_value().encode(),
        value.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_secret(value: str, expected_hash: str, settings: Settings) -> bool:
    """Compare in constant time, so the comparison leaks nothing by timing."""
    return hmac.compare_digest(hash_secret(value, settings), expected_hash)


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------
def create_access_token(
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Sign an access token. Returns the token and the moment it expires.

    The session ID travels in the token so that logging out one device can be
    enforced on the access token too, not only on its refresh token. Nothing
    personal goes in the payload: a JWT is readable by anyone holding it, and the
    claims below are opaque identifiers.
    """
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + settings.access_token_ttl
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": TOKEN_TYPE_ACCESS,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "iss": settings.app_name,
    }
    token = jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_at


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    """Verify and parse an access token, or raise `UnauthenticatedError`.

    The algorithm is pinned to the configured one. Accepting whatever the token's
    own header asks for is the classic JWT vulnerability: it lets a caller present
    `alg: none`, or have an RSA public key treated as an HMAC secret.
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.app_name,
            options={"require": ["exp", "iat", "sub", "sid", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthenticatedError("The access token has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Deliberately one message for every other failure. Telling a caller
        # whether a token was malformed, forged, or for another issuer helps only
        # the caller forging them.
        raise UnauthenticatedError("The access token is not valid") from exc

    if payload.get("typ") != TOKEN_TYPE_ACCESS:
        raise UnauthenticatedError("The access token is not valid")

    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            token_type=TOKEN_TYPE_ACCESS,
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        raise UnauthenticatedError("The access token is not valid") from exc


def refresh_token_expiry(settings: Settings, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + settings.refresh_token_ttl


def expires_in_seconds(expires_at: datetime, now: datetime | None = None) -> int:
    """Whole seconds until expiry, never negative.

    Clients need a number to schedule a refresh against; a negative one would
    have them refresh in a loop.
    """
    delta = expires_at - (now or datetime.now(UTC))
    return max(0, int(delta // timedelta(seconds=1)))
