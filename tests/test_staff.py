"""Staff accounts: credentials, sign-in, token separation, staff management."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import UnauthenticatedError
from app.core.security import decode_access_token
from app.modules.moderation.models import AuditAction, AuditEvent
from app.modules.staff import credentials
from app.modules.staff.models import StaffAccount, StaffRole, StaffSession
from app.modules.users.models import User
from tests.factories import make_staff, staff_token

SIGN_IN = "/v1/admin/auth/sign-in"


def _code(secret: str, offset: int = 0) -> str:
    return credentials.totp_code(secret, credentials.totp_step() + offset)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def test_password_hash_round_trip() -> None:
    encoded = credentials.hash_password("correct horse battery")
    assert encoded.startswith("scrypt$32768$8$1$")
    assert credentials.verify_password("correct horse battery", encoded)
    assert not credentials.verify_password("correct horse batterY", encoded)
    # Two hashes of one password differ: the salt is random.
    assert encoded != credentials.hash_password("correct horse battery")


@pytest.mark.parametrize("encoded", ["", "scrypt$1$2", "bcrypt$1$1$1$00$00", "scrypt$x$8$1$00$00"])
def test_malformed_hash_is_a_mismatch(encoded: str) -> None:
    assert not credentials.verify_password("anything", encoded)


def test_totp_matches_rfc_6238_vector() -> None:
    # RFC 6238 appendix B, SHA-1, T = 59 s, secret "12345678901234567890": the
    # eight-digit 94287082, truncated to six.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert credentials.totp_code(secret, 59 // 30) == "287082"


def test_totp_allows_one_step_of_drift_and_no_more() -> None:
    secret = credentials.generate_totp_secret()
    now = datetime.now(UTC)
    step = credentials.totp_step(now)
    for offset in (-1, 0, 1):
        code = credentials.totp_code(secret, step + offset)
        assert credentials.verify_totp(secret, code, last_step=None, now=now) == step + offset
    for offset in (-2, 2):
        code = credentials.totp_code(secret, step + offset)
        assert credentials.verify_totp(secret, code, last_step=None, now=now) is None


def test_totp_code_is_good_once() -> None:
    secret = credentials.generate_totp_secret()
    now = datetime.now(UTC)
    step = credentials.totp_step(now)
    code = credentials.totp_code(secret, step)
    assert credentials.verify_totp(secret, code, last_step=step, now=now) is None
    assert credentials.verify_totp(secret, code, last_step=step - 1, now=now) == step


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef"])
def test_totp_rejects_malformed_codes(code: str) -> None:
    assert credentials.verify_totp("JBSWY3DPEHPK3PXP", code, last_step=None) is None


def test_provisioning_uri() -> None:
    uri = credentials.provisioning_uri(
        secret="JBSWY3DPEHPK3PXP", email="a@example.com", issuer="MyRadius staff"
    )
    assert uri.startswith("otpauth://totp/MyRadius%20staff%3Aa%40example.com?")
    assert "secret=JBSWY3DPEHPK3PXP" in uri


# ---------------------------------------------------------------------------
# Token separation
# ---------------------------------------------------------------------------
async def test_staff_token_is_not_a_user_token(
    db_session: AsyncSession, settings: Settings
) -> None:
    issued = await make_staff(db_session)
    token = await staff_token(db_session, issued.account)
    with pytest.raises(UnauthenticatedError):
        decode_access_token(token, settings)


async def test_user_token_is_refused_by_the_admin_api(
    client: httpx.AsyncClient,
    authenticate: Callable[[User], Awaitable[httpx.AsyncClient]],
    user: User,
) -> None:
    await authenticate(user)
    response = await client.get("/v1/admin/me")
    assert response.status_code == 401


async def test_staff_token_is_refused_by_user_endpoints(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued = await make_staff(db_session, role=StaffRole.admin)
    token = await staff_token(db_session, issued.account)
    response = await client.get("/v1/me", headers=_bearer(token))
    assert response.status_code == 401


async def test_admin_api_needs_a_token(client: httpx.AsyncClient) -> None:
    client.headers.pop("Authorization", None)
    assert (await client.get("/v1/admin/me")).status_code == 401


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
async def test_sign_in_with_all_three_factors(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued = await make_staff(db_session)
    response = await client.post(
        SIGN_IN,
        json={
            "email": issued.account.email.upper(),
            "password": issued.password,
            "code": _code(issued.totp_secret),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["staff"]["email"] == issued.account.email
    me = await client.get("/v1/admin/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["role"] == "moderator"

    audited = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.actor_id == issued.account.id,
                AuditEvent.action == AuditAction.staff_signed_in,
            )
        )
    ).scalar_one()
    assert audited.actor_type == "moderator"


async def test_every_failure_gets_the_same_answer(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued = await make_staff(db_session)
    attempts = [
        {"email": "nobody@example.com", "password": issued.password, "code": "123456"},
        {"email": issued.account.email, "password": "wrong-password", "code": "123456"},
        {"email": issued.account.email, "password": issued.password, "code": "000000"},
    ]
    answers = set()
    for attempt in attempts:
        response = await client.post(SIGN_IN, json=attempt)
        assert response.status_code == 401
        answers.add(response.json()["error"]["message"])
    assert len(answers) == 1


async def test_a_code_cannot_be_replayed(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued = await make_staff(db_session)
    payload = {
        "email": issued.account.email,
        "password": issued.password,
        "code": _code(issued.totp_secret),
    }
    assert (await client.post(SIGN_IN, json=payload)).status_code == 200
    assert (await client.post(SIGN_IN, json=payload)).status_code == 401


async def test_repeated_failures_lock_the_account(
    client: httpx.AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    issued = await make_staff(db_session)
    account_id = issued.account.id
    for _ in range(settings.staff_max_failed_logins):
        response = await client.post(
            SIGN_IN,
            json={"email": issued.account.email, "password": "wrong", "code": "123456"},
        )
        assert response.status_code == 401

    # The right credentials, now refused, with the same message.
    response = await client.post(
        SIGN_IN,
        json={
            "email": issued.account.email,
            "password": issued.password,
            "code": _code(issued.totp_secret),
        },
    )
    assert response.status_code == 401
    account = await db_session.get(StaffAccount, account_id)
    assert account is not None
    await db_session.refresh(account)
    assert account.locked_until is not None
    assert account.locked_until > datetime.now(UTC)

    # Once the lock lapses, they are let in.
    account.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    response = await client.post(
        SIGN_IN,
        json={
            "email": issued.account.email,
            "password": issued.password,
            "code": _code(issued.totp_secret, offset=1),
        },
    )
    assert response.status_code == 200, response.text


async def test_failures_are_audited(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    issued = await make_staff(db_session)
    await client.post(
        SIGN_IN, json={"email": issued.account.email, "password": "wrong", "code": "123456"}
    )
    failures = (
        (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.actor_id == issued.account.id,
                    AuditEvent.action == AuditAction.staff_sign_in_failed,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(failures) == 1
    assert failures[0].context == {"failure": "password", "locked": False}


async def test_disabled_account_cannot_sign_in(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued = await make_staff(db_session)
    issued.account.disabled_at = datetime.now(UTC)
    await db_session.flush()
    response = await client.post(
        SIGN_IN,
        json={
            "email": issued.account.email,
            "password": issued.password,
            "code": _code(issued.totp_secret),
        },
    )
    assert response.status_code == 401


async def test_sign_out_ends_the_session(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued = await make_staff(db_session)
    token = await staff_token(db_session, issued.account)
    assert (await client.post("/v1/admin/auth/sign-out", headers=_bearer(token))).status_code == 204
    assert (await client.get("/v1/admin/me", headers=_bearer(token))).status_code == 401


async def test_expired_session_is_refused(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    issued = await make_staff(db_session)
    token = await staff_token(db_session, issued.account)
    staff_session = (
        await db_session.execute(
            select(StaffSession).where(StaffSession.staff_id == issued.account.id)
        )
    ).scalar_one()
    staff_session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert (await client.get("/v1/admin/me", headers=_bearer(token))).status_code == 401


async def test_change_password(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    issued = await make_staff(db_session)
    token = await staff_token(db_session, issued.account)
    too_short = await client.post(
        "/v1/admin/me/password",
        headers=_bearer(token),
        json={
            "current_password": issued.password,
            "new_password": "short",
            "code": _code(issued.totp_secret),
        },
    )
    assert too_short.status_code == 400
    response = await client.post(
        "/v1/admin/me/password",
        headers=_bearer(token),
        json={
            "current_password": issued.password,
            "new_password": "a much longer passphrase",
            "code": _code(issued.totp_secret),
        },
    )
    assert response.status_code == 204, response.text
    account = await db_session.get(StaffAccount, issued.account.id)
    assert account is not None
    await db_session.refresh(account)
    assert credentials.verify_password("a much longer passphrase", account.password_hash)


# ---------------------------------------------------------------------------
# Staff management
# ---------------------------------------------------------------------------
async def test_admin_creates_a_moderator_who_can_sign_in(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_staff(db_session, role=StaffRole.admin)
    token = await staff_token(db_session, admin.account)
    created = await client.post(
        "/v1/admin/staff",
        headers=_bearer(token),
        json={"email": "New.Mod@Example.com", "display_name": "New Mod"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["staff"]["email"] == "new.mod@example.com"
    assert body["staff"]["role"] == "moderator"
    assert body["totp_uri"].startswith("otpauth://totp/")

    client.headers.pop("Authorization", None)
    response = await client.post(
        SIGN_IN,
        json={
            "email": "new.mod@example.com",
            "password": body["temporary_password"],
            "code": _code(body["totp_secret"]),
        },
    )
    assert response.status_code == 200, response.text

    duplicate = await client.post(
        "/v1/admin/staff",
        headers=_bearer(token),
        json={"email": "new.mod@example.com", "display_name": "Again"},
    )
    assert duplicate.status_code == 409


async def test_moderators_cannot_manage_staff(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    moderator = await make_staff(db_session)
    token = await staff_token(db_session, moderator.account)
    assert (await client.get("/v1/admin/staff", headers=_bearer(token))).status_code == 403
    response = await client.post(
        "/v1/admin/staff",
        headers=_bearer(token),
        json={"email": "x@example.com", "display_name": "X"},
    )
    assert response.status_code == 403


async def test_the_last_admin_is_protected(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_staff(db_session, role=StaffRole.admin)
    token = await staff_token(db_session, admin.account)
    url = f"/v1/admin/staff/{admin.account.id}"
    demote = await client.patch(url, headers=_bearer(token), json={"role": "moderator"})
    assert demote.status_code == 400
    disable = await client.patch(url, headers=_bearer(token), json={"disabled": True})
    assert disable.status_code == 400


async def test_disabling_an_account_ends_its_sessions(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_staff(db_session, role=StaffRole.admin)
    moderator = await make_staff(db_session)
    admin_token = await staff_token(db_session, admin.account)
    moderator_token = await staff_token(db_session, moderator.account)
    response = await client.patch(
        f"/v1/admin/staff/{moderator.account.id}",
        headers=_bearer(admin_token),
        json={"disabled": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["disabled"] is True
    assert (await client.get("/v1/admin/me", headers=_bearer(moderator_token))).status_code == 401


async def test_reset_credentials_replaces_both_factors(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await make_staff(db_session, role=StaffRole.admin)
    moderator = await make_staff(db_session)
    old_secret = moderator.totp_secret
    token = await staff_token(db_session, admin.account)
    response = await client.post(
        f"/v1/admin/staff/{moderator.account.id}/reset-credentials", headers=_bearer(token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totp_secret"] != old_secret
    assert body["temporary_password"] != moderator.password
