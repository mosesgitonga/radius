"""User endpoints: profile, consent, blocking, and data rights."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.auth import service as auth_service
from app.modules.auth.models import DeviceSession
from app.modules.moderation.models import AuditAction, AuditEvent
from app.modules.users.models import ConsentType, User, UserStatus
from app.modules.users.service import blocked_user_ids
from tests.factories import KARATINA, NYERI_TOWN, make_user, offset_km, phone_of

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


# ---------------------------------------------------------------------------
# My profile
# ---------------------------------------------------------------------------
async def test_my_profile_includes_my_own_phone_number(
    auth_client: httpx.AsyncClient, user: User
) -> None:
    """The caller's own data, shown on their account screen."""
    response = await auth_client.get("/v1/me")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user.id)
    assert body["phone_number"] == user.phone_number
    assert body["display_name"] == user.display_name


async def test_my_profile_has_no_coordinates(auth_client: httpx.AsyncClient) -> None:
    """Even for the caller: the server has none to give."""
    body = (await auth_client.get("/v1/me")).json()

    for forbidden in ("latitude", "longitude", "centroid", "cell", "home_cell_res9"):
        assert forbidden not in body


async def test_my_profile_shows_badge_and_rating_placeholders(
    auth_client: httpx.AsyncClient,
) -> None:
    """Present from Phase 1 so the client can build the screen once."""
    body = (await auth_client.get("/v1/me")).json()

    assert body["is_verified_neighbour"] is False
    assert body["rating_count"] == 0
    assert body["rating_average"] is None


async def test_my_profile_requires_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/me")).status_code == 401


async def test_i_can_change_my_display_name(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, user: User
) -> None:
    response = await auth_client.patch("/v1/me", json={"display_name": "Mama Mboga"})

    assert response.status_code == 200
    assert response.json()["display_name"] == "Mama Mboga"
    await db_session.refresh(user)
    assert user.display_name == "Mama Mboga"


async def test_a_blank_display_name_is_refused(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.patch("/v1/me", json={"display_name": "   "})

    assert response.status_code in (400, 422)


async def test_changing_a_profile_is_audited_without_the_new_value(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, user: User
) -> None:
    """Which fields changed, not what they changed to: a display name is personal
    data and the audit log is kept for two years."""
    await auth_client.patch("/v1/me", json={"display_name": "Mama Mboga"})

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.profile_updated,
                    AuditEvent.actor_id == user.id,
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(events) == 1
    assert events[0].context == {"fields": ["display_name"]}
    assert "Mama Mboga" not in str(events[0].context)


# ---------------------------------------------------------------------------
# Public profiles
# ---------------------------------------------------------------------------
async def test_a_public_profile_shows_a_distance_band_not_a_distance(
    auth_client: httpx.AsyncClient, other_user: User
) -> None:
    response = await auth_client.get(f"/v1/users/{other_user.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["distance_bucket"] == "1_3km"
    assert "distance_km" not in body
    assert "distance_m" not in body


async def test_a_public_profile_omits_the_phone_number(
    auth_client: httpx.AsyncClient, other_user: User
) -> None:
    """Someone else's number is not the caller's business, and in a
    neighbourhood app it is how harassment starts."""
    body = (await auth_client.get(f"/v1/users/{other_user.id}")).json()

    assert "phone_number" not in body
    assert phone_of(other_user) not in str(body)


async def test_a_far_away_neighbour_is_in_the_furthest_band(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    distant = await make_user(db_session, at=KARATINA)

    body = (await auth_client.get(f"/v1/users/{distant.id}")).json()

    assert body["distance_bucket"] == "10km_plus"


async def test_a_neighbour_with_no_location_has_no_band(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    nowhere = await make_user(db_session)

    body = (await auth_client.get(f"/v1/users/{nowhere.id}")).json()

    assert body["distance_bucket"] is None
    assert body["area_label"] is None


async def test_an_unknown_user_is_not_found(auth_client: httpx.AsyncClient) -> None:
    import uuid

    response = await auth_client.get(f"/v1/users/{uuid.uuid4()}")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
async def test_my_consents_are_listed_with_versions(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    user: User,
) -> None:
    from app.modules.users.service import set_consent

    await set_consent(
        db_session,
        user=user,
        consent_type=ConsentType.marketing,
        granted=True,
        settings=settings,
    )
    await db_session.flush()

    response = await auth_client.get("/v1/me/consents")

    assert response.status_code == 200
    marketing = [c for c in response.json() if c["consent_type"] == "marketing"]
    assert marketing[0]["granted"] is True
    assert marketing[0]["document_version"] == settings.privacy_policy_version


async def test_marketing_consent_can_be_granted_and_withdrawn(
    auth_client: httpx.AsyncClient,
) -> None:
    granted = await auth_client.put(
        "/v1/me/consents", json={"consent_type": "marketing", "granted": True}
    )
    withdrawn = await auth_client.put(
        "/v1/me/consents", json={"consent_type": "marketing", "granted": False}
    )

    assert granted.json()["granted"] is True
    assert withdrawn.json()["granted"] is False


async def test_withdrawing_consent_keeps_the_earlier_grant_as_evidence(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, user: User
) -> None:
    """Append-only: a withdrawal is itself a fact worth keeping, and the Act asks
    what somebody agreed to and when, not only what is true now."""
    from app.modules.users.models import Consent

    await auth_client.put("/v1/me/consents", json={"consent_type": "marketing", "granted": True})
    await auth_client.put("/v1/me/consents", json={"consent_type": "marketing", "granted": False})

    rows = (
        (
            await db_session.execute(
                select(Consent).where(
                    Consent.user_id == user.id,
                    Consent.consent_type == ConsentType.marketing,
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 2
    assert {row.granted for row in rows} == {True, False}


@pytest.mark.parametrize("required", ["terms", "privacy_policy", "location_use"])
async def test_required_consents_cannot_be_withdrawn_here(
    auth_client: httpx.AsyncClient, required: str
) -> None:
    """Rejected by the schema: the features they cover are the product, so there
    is no toggle that would quietly half-break it."""
    response = await auth_client.put(
        "/v1/me/consents", json={"consent_type": required, "granted": False}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------
async def test_blocking_a_neighbour(auth_client: httpx.AsyncClient, other_user: User) -> None:
    response = await auth_client.post("/v1/me/blocks", json={"user_id": str(other_user.id)})

    assert response.status_code == 201
    assert response.json()["user_id"] == str(other_user.id)


async def test_blocking_twice_succeeds(auth_client: httpx.AsyncClient, other_user: User) -> None:
    """Idempotent, so a client retrying on a flaky connection does not get an
    error for an outcome that already holds."""
    first = await auth_client.post("/v1/me/blocks", json={"user_id": str(other_user.id)})
    second = await auth_client.post("/v1/me/blocks", json={"user_id": str(other_user.id)})

    assert first.status_code == 201
    assert second.status_code == 201


async def test_i_cannot_block_myself(auth_client: httpx.AsyncClient, user: User) -> None:
    response = await auth_client.post("/v1/me/blocks", json={"user_id": str(user.id)})

    assert response.status_code == 400


async def test_my_blocks_are_listed(auth_client: httpx.AsyncClient, other_user: User) -> None:
    await auth_client.post("/v1/me/blocks", json={"user_id": str(other_user.id)})

    response = await auth_client.get("/v1/me/blocks")

    assert response.status_code == 200
    assert [b["user_id"] for b in response.json()] == [str(other_user.id)]


async def test_the_block_list_does_not_reveal_who_blocked_me(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    user: User,
    other_user: User,
    authenticate: Authenticate,
) -> None:
    """Telling someone they have been blocked hands them information they have no
    business having, and an obvious next move."""
    blocker_client = await authenticate(other_user)
    await blocker_client.post("/v1/me/blocks", json={"user_id": str(user.id)})

    victim_client = await authenticate(user)
    response = await victim_client.get("/v1/me/blocks")

    assert response.json() == []


async def test_unblocking_removes_the_block(
    auth_client: httpx.AsyncClient, other_user: User
) -> None:
    await auth_client.post("/v1/me/blocks", json={"user_id": str(other_user.id)})

    response = await auth_client.delete(f"/v1/me/blocks/{other_user.id}")

    assert response.status_code == 204
    assert (await auth_client.get("/v1/me/blocks")).json() == []


async def test_unblocking_someone_not_blocked_is_not_found(
    auth_client: httpx.AsyncClient, other_user: User
) -> None:
    response = await auth_client.delete(f"/v1/me/blocks/{other_user.id}")

    assert response.status_code == 404


async def test_blocking_is_audited(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, other_user: User
) -> None:
    await auth_client.post("/v1/me/blocks", json={"user_id": str(other_user.id)})

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.user_blocked)
            )
        )
        .scalars()
        .all()
    )

    assert len(events) == 1
    assert events[0].target_id == other_user.id


# ---------------------------------------------------------------------------
# Data rights: export
# ---------------------------------------------------------------------------
async def test_the_export_contains_the_account(auth_client: httpx.AsyncClient, user: User) -> None:
    response = await auth_client.get("/v1/me/export")

    assert response.status_code == 200
    body = response.json()
    assert body["account"]["id"] == str(user.id)
    assert body["account"]["phone_number"] == user.phone_number
    assert body["export_format_version"] == 1


async def test_the_export_contains_consents_and_blocks(
    auth_client: httpx.AsyncClient, other_user: User
) -> None:
    await auth_client.put("/v1/me/consents", json={"consent_type": "marketing", "granted": True})
    await auth_client.post("/v1/me/blocks", json={"user_id": str(other_user.id)})

    body = (await auth_client.get("/v1/me/export")).json()

    assert any(c["type"] == "marketing" for c in body["consents"])
    assert [b["blocked_user_id"] for b in body["blocks"]] == [str(other_user.id)]


async def test_the_export_hands_back_no_coordinates(
    auth_client: httpx.AsyncClient,
) -> None:
    """The right of access is to the data held — and what is held is a cell, not
    a position. Returning coordinates would recreate the precision the system
    exists to avoid."""
    body = (await auth_client.get("/v1/me/export")).json()

    rendered = str(body)
    assert "latitude" not in rendered
    assert "longitude" not in rendered
    assert "centroid" not in rendered
    assert body["location"]["home_area_set"] is True


async def test_the_export_says_what_is_not_held(
    auth_client: httpx.AsyncClient,
) -> None:
    """ "We keep no location history" is a more useful answer than silence."""
    body = (await auth_client.get("/v1/me/export")).json()

    assert "location history" in body["not_held"]
    assert "raw GPS coordinates" in body["not_held"]


async def test_exporting_is_audited(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await auth_client.get("/v1/me/export")

    events = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.data_exported)
            )
        )
        .scalars()
        .all()
    )

    assert len(events) == 1


# ---------------------------------------------------------------------------
# Data rights: deletion
# ---------------------------------------------------------------------------
async def test_requesting_deletion_starts_a_grace_period(
    auth_client: httpx.AsyncClient, settings: Settings
) -> None:
    response = await auth_client.post("/v1/me/deletion-request")

    assert response.status_code == 202
    body = response.json()
    requested = datetime.fromisoformat(body["requested_at"])
    purge_after = datetime.fromisoformat(body["purge_after"])
    assert (purge_after - requested).days == settings.deletion_grace_period_days


async def test_requesting_deletion_revokes_every_session(
    auth_client: httpx.AsyncClient, db_session: AsyncSession, user: User
) -> None:
    """Someone deleting their account has usually decided they do not want it
    reachable; leaving tokens live for a month would not honour that."""
    await auth_client.post("/v1/me/deletion-request")

    sessions = (
        (await db_session.execute(select(DeviceSession).where(DeviceSession.user_id == user.id)))
        .scalars()
        .all()
    )

    assert sessions
    assert all(session.revoked_at is not None for session in sessions)


async def test_requesting_deletion_twice_is_a_conflict(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    user: User,
) -> None:
    await auth_client.post("/v1/me/deletion-request")
    # The first request revoked the session, so a fresh one is needed.
    pair = await auth_service.issue_tokens(db_session, user=user, settings=settings)
    await db_session.flush()
    auth_client.headers["Authorization"] = f"Bearer {pair.access_token}"

    second = await auth_client.post("/v1/me/deletion-request")

    assert second.status_code == 409


async def test_deletion_can_be_cancelled(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    user: User,
) -> None:
    """The grace period exists because deletion is frequently regretted, and
    sometimes coerced."""
    await auth_client.post("/v1/me/deletion-request")
    pair = await auth_service.issue_tokens(db_session, user=user, settings=settings)
    await db_session.flush()
    auth_client.headers["Authorization"] = f"Bearer {pair.access_token}"

    response = await auth_client.delete("/v1/me/deletion-request")

    assert response.status_code == 204
    await db_session.refresh(user)
    assert user.deletion_requested_at is None
    assert user.status == UserStatus.active


async def test_a_pending_deletion_blocks_writes_but_not_reads(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    user: User,
) -> None:
    """Suspended and deletion-pending accounts keep read access on purpose: they
    are still entitled to see their data and to cancel."""
    await auth_client.post("/v1/me/deletion-request")
    pair = await auth_service.issue_tokens(db_session, user=user, settings=settings)
    await db_session.flush()
    auth_client.headers["Authorization"] = f"Bearer {pair.access_token}"

    assert (await auth_client.get("/v1/me")).status_code == 200
    assert (await auth_client.get("/v1/me/export")).status_code == 200
    assert (await auth_client.patch("/v1/me", json={"display_name": "Nope"})).status_code == 403


# ---------------------------------------------------------------------------
# Account status
# ---------------------------------------------------------------------------
async def test_a_suspended_account_can_read_but_not_write(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    suspended = await make_user(db_session, at=NYERI_TOWN, status=UserStatus.suspended)
    suspended_client = await authenticate(suspended)

    assert (await suspended_client.get("/v1/me")).status_code == 200
    assert (
        await suspended_client.patch("/v1/me", json={"display_name": "Nope"})
    ).status_code == 403


async def test_a_banned_account_cannot_do_anything(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    banned = await make_user(db_session, at=NYERI_TOWN, status=UserStatus.banned)
    banned_client = await authenticate(banned)

    assert (await banned_client.get("/v1/me")).status_code == 403


async def test_a_soft_deleted_account_cannot_authenticate(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    gone = await make_user(db_session, at=NYERI_TOWN)
    gone_client = await authenticate(gone)
    gone.deleted_at = datetime.now(UTC)
    await db_session.flush()

    assert (await gone_client.get("/v1/me")).status_code == 401


# ---------------------------------------------------------------------------
# The shared blocking rule
# ---------------------------------------------------------------------------
async def test_the_block_set_includes_both_directions(
    db_session: AsyncSession, user: User, other_user: User
) -> None:
    """The function every later module filters through. A block is recorded
    one-way so only the blocker can undo it, but it must take effect both ways —
    otherwise the blocked person keeps watching and messaging, which is exactly
    what the blocker was trying to stop."""
    from app.modules.users.service import block_user

    await block_user(db_session, blocker=user, blocked_id=other_user.id)
    await db_session.flush()

    assert await blocked_user_ids(db_session, user.id) == {other_user.id}
    assert await blocked_user_ids(db_session, other_user.id) == {user.id}


async def test_the_block_set_excludes_the_viewer(
    db_session: AsyncSession, user: User, other_user: User
) -> None:
    """A viewer appearing in their own block set would hide their own content."""
    from app.modules.users.service import block_user

    await block_user(db_session, blocker=user, blocked_id=other_user.id)
    await db_session.flush()

    assert user.id not in await blocked_user_ids(db_session, user.id)


async def test_an_unrelated_user_is_not_blocked(
    db_session: AsyncSession, user: User, other_user: User
) -> None:
    stranger = await make_user(db_session, at=offset_km(NYERI_TOWN, north=1.0))

    assert await blocked_user_ids(db_session, stranger.id) == set()


async def test_rate_limit_headers_are_returned(
    auth_client: httpx.AsyncClient,
) -> None:
    """So clients can back off before they are refused."""
    response = await auth_client.patch("/v1/me", json={"display_name": "Someone"})

    assert "X-RateLimit-Limit" in response.headers
    assert "X-RateLimit-Remaining" in response.headers


async def test_exceeding_a_per_user_limit_returns_429(
    auth_client: httpx.AsyncClient,
) -> None:
    statuses = []
    for index in range(12):
        response = await auth_client.patch("/v1/me", json={"display_name": f"Neighbour {index}"})
        statuses.append(response.status_code)

    assert 429 in statuses
    limited = [s for s in statuses if s == 429]
    assert len(limited) >= 1
