"""Staff credentials: passwords, authenticator codes, and staff tokens.

Everything here is standard library apart from the JWT, which the user tokens
already use.

**Passwords use scrypt.** Unlike the secrets in `core.security` — random tokens and
rate-limited codes, which a keyed HMAC suits because they must be looked up — a
password is chosen by a person and is guessable, so it needs a deliberately slow,
memory-hard, salted KDF. The parameters are written into each hash, so they can be
raised later and old hashes still verify.

**Codes are TOTP (RFC 6238)**: HMAC-SHA1, six digits, thirty-second steps, which is
what every authenticator app speaks. One step of clock drift either way is
allowed, and a step is good once: the caller passes the last accepted step and a
code for it or anything earlier is refused, so an overheard code cannot be
replayed inside its window.

**Staff tokens are a different kind of token**, with their own `typ` and audience.
`core.security.decode_access_token` refuses them because the type is wrong, and
`decode_staff_token` refuses user tokens for the same reason — tested both ways.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets
import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote, urlencode

import jwt

from app.core.config import Settings
from app.core.errors import UnauthenticatedError

TOKEN_TYPE_STAFF: Final = "staff"  # noqa: S105 — a token-type tag, not a secret
STAFF_AUDIENCE: Final = "staff"

# scrypt at N=2^15, r=8: 32 MiB and roughly 50-100 ms per hash on a small VPS. A
# sign-in is rare and rate-limited; a guess against a stolen hash is not.
SCRYPT_N: Final = 2**15
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SCRYPT_DKLEN: Final = 32
_SCRYPT_MAXMEM: Final = 64 * 1024 * 1024

TOTP_DIGITS: Final = 6
TOTP_STEP_SECONDS: Final = 30
TOTP_DRIFT_STEPS: Final = 1
TOTP_SECRET_BYTES: Final = 20


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def _scrypt(password: str, salt: bytes, *, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=_SCRYPT_MAXMEM
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a password against a stored hash. A malformed hash
    is a mismatch, never an exception."""
    try:
        scheme, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = _scrypt(
            password, bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


@functools.cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    """Check against a real hash of a random password, for an unknown email, so
    that a wrong email costs the same time as a wrong password."""
    verify_password(password, _dummy_hash())


def generate_password() -> str:
    """A temporary password for a new or reset account: 20 URL-safe characters."""
    return secrets.token_urlsafe(15)


# ---------------------------------------------------------------------------
# Authenticator codes
# ---------------------------------------------------------------------------
def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode().rstrip("=")


def _secret_bytes(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def totp_step(now: datetime | None = None) -> int:
    return int((now or datetime.now(UTC)).timestamp()) // TOTP_STEP_SECONDS


def totp_code(secret: str, step: int) -> str:
    digest = hmac.new(_secret_bytes(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def verify_totp(
    secret: str, code: str, *, last_step: int | None, now: datetime | None = None
) -> int | None:
    """The step `code` is valid for, or None.

    Only steps after `last_step` are considered, so each code works once. The
    newest matching step is returned for the caller to store.
    """
    code = code.strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = totp_step(now)
    matched: int | None = None
    for step in range(current - TOTP_DRIFT_STEPS, current + TOTP_DRIFT_STEPS + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp_code(secret, step), code):
            matched = step
    return matched


def provisioning_uri(*, secret: str, email: str, issuer: str) -> str:
    """The `otpauth://` URI an authenticator app reads from a QR code."""
    label = quote(f"{issuer}:{email}")
    query = urlencode({"secret": secret, "issuer": issuer, "digits": TOTP_DIGITS})
    return f"otpauth://totp/{label}?{query}"


# ---------------------------------------------------------------------------
# Staff tokens
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StaffTokenClaims:
    staff_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


def create_staff_token(
    *, staff_id: uuid.UUID, session_id: uuid.UUID, expires_at: datetime, settings: Settings
) -> str:
    payload: dict[str, Any] = {
        "sub": str(staff_id),
        "sid": str(session_id),
        "typ": TOKEN_TYPE_STAFF,
        "aud": STAFF_AUDIENCE,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "iss": settings.app_name,
    }
    return jwt.encode(
        payload, settings.secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )


def decode_staff_token(token: str, settings: Settings) -> StaffTokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.app_name,
            audience=STAFF_AUDIENCE,
            options={"require": ["exp", "iat", "sub", "sid", "typ", "aud"]},
        )
        if payload.get("typ") != TOKEN_TYPE_STAFF:
            raise jwt.InvalidTokenError("wrong token type")
        return StaffTokenClaims(
            staff_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthenticatedError("The staff session has expired") from exc
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise UnauthenticatedError("The staff token is not valid") from exc
