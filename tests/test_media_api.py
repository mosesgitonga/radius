"""Media endpoints and the processing job."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers.storage import MemoryStorageProvider
from app.core.queue import RecordingJobQueue
from app.modules.media import service as media_service
from app.modules.media.images import has_exif, read_gps_tags
from app.modules.media.models import MediaPurpose, MediaStatus
from app.modules.users.models import User
from app.workers.tasks import catch_up_media, process_media
from tests.factories import NYERI_TOWN, jpeg_with_gps, make_ready_image, make_user

Authenticate = Callable[[User], Awaitable[httpx.AsyncClient]]


async def start_upload(client: httpx.AsyncClient, purpose: str = "post") -> dict[str, object]:
    response = await client.post(
        "/v1/media/uploads", json={"purpose": purpose, "content_type": "image/jpeg"}
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def key_from_ticket(ticket: dict[str, object]) -> str:
    return str(ticket["upload_url"]).removeprefix("memory://upload/")


# ---------------------------------------------------------------------------
# The upload flow
# ---------------------------------------------------------------------------
async def test_starting_an_upload_returns_a_presigned_url(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)

    ticket = await start_upload(await authenticate(user))

    assert ticket["upload_method"] == "PUT"
    assert ticket["upload_headers"] == {"Content-Type": "image/jpeg"}
    assert ticket["max_bytes"] == 12 * 1024 * 1024


async def test_the_full_flow_ends_with_metadata_free_variants(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    queue: RecordingJobQueue,
    authenticate: Authenticate,
) -> None:
    """Presign, upload, confirm, process, fetch — the path a real phone takes."""
    user = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(user)
    ticket = await start_upload(client)
    # The client would PUT to the presigned URL; the memory store stands in.
    await storage.put_object(
        storage_key=key_from_ticket(ticket), data=jpeg_with_gps(), content_type="image/jpeg"
    )

    confirmed = await client.post(f"/v1/media/{ticket['media_id']}/complete")
    assert confirmed.status_code == 202
    assert confirmed.json()["status"] == "processing"
    assert queue.names() == ["process_media"]

    # The worker task, called directly, in the test's own session.
    status = await process_media({"job_id": "t", "session": db_session}, str(ticket["media_id"]))
    assert status == "ready"

    fetched = (await client.get(f"/v1/media/{ticket['media_id']}")).json()
    assert fetched["status"] == "ready"
    assert set(fetched["variants"]) == {"320", "720", "1280"}
    for variant in fetched["variants"].values():
        data, _ = storage.objects[variant["url"].removeprefix("memory://object/")]
        assert read_gps_tags(data) == {}
        assert not has_exif(data)


async def test_confirming_before_uploading_is_refused(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    """Otherwise the asset would sit in `processing` until a human noticed."""
    user = await make_user(db_session, at=NYERI_TOWN)
    client = await authenticate(user)
    ticket = await start_upload(client)

    response = await client.post(f"/v1/media/{ticket['media_id']}/complete")

    assert response.status_code == 400


async def test_confirming_twice_is_safe(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    asset = await make_ready_image(db_session, storage, owner=user)

    response = await (await authenticate(user)).post(f"/v1/media/{asset.id}/complete")

    assert response.status_code == 202
    assert response.json()["status"] == "ready"


async def test_an_unsupported_type_is_refused_at_presign(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)

    response = await (await authenticate(user)).post(
        "/v1/media/uploads", json={"purpose": "post", "content_type": "image/svg+xml"}
    )

    assert response.status_code == 400


async def test_a_bad_upload_is_rejected_and_says_why(
    db_session: AsyncSession, storage: MemoryStorageProvider, settings: object
) -> None:
    from app.core.config import get_settings

    user = await make_user(db_session, at=NYERI_TOWN)
    ticket = await media_service.create_upload(
        db_session,
        storage,
        owner_id=user.id,
        purpose=MediaPurpose.post,
        content_type="image/jpeg",
        settings=get_settings(),
    )
    assert ticket.asset.original_key is not None
    await storage.put_object(
        storage_key=ticket.asset.original_key, data=b"not an image", content_type="image/jpeg"
    )
    asset = await media_service.mark_uploaded(db_session, storage, asset=ticket.asset)

    asset = await media_service.process_asset(
        db_session, storage, asset=asset, settings=get_settings()
    )

    assert asset.status == MediaStatus.rejected
    assert asset.rejection_code == "unsupported_format"
    assert asset.rejection_reason
    # The rejected original is not left lying around either.
    assert storage.objects == {}


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------
async def test_someone_elses_upload_is_not_found(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    """404 rather than 403, so media IDs cannot be enumerated."""
    owner = await make_user(db_session, at=NYERI_TOWN)
    other = await make_user(db_session, at=NYERI_TOWN)
    asset = await make_ready_image(db_session, storage, owner=owner)

    response = await (await authenticate(other)).get(f"/v1/media/{asset.id}")

    assert response.status_code == 404


async def test_an_unknown_media_id_is_not_found(
    db_session: AsyncSession, authenticate: Authenticate
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)

    response = await (await authenticate(user)).get(f"/v1/media/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_media_responses_never_expose_storage_keys(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    """Only processed variants are addressable, and only by URL."""
    user = await make_user(db_session, at=NYERI_TOWN)
    asset = await make_ready_image(db_session, storage, owner=user)

    body = (await (await authenticate(user)).get(f"/v1/media/{asset.id}")).json()

    assert "original_key" not in str(body)
    assert "original/" not in str(body)


# ---------------------------------------------------------------------------
# Avatars
# ---------------------------------------------------------------------------
async def test_setting_an_avatar(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    avatar = await make_ready_image(db_session, storage, owner=user, purpose=MediaPurpose.avatar)
    client = await authenticate(user)

    response = await client.put("/v1/me/avatar", json={"media_id": str(avatar.id)})

    assert response.status_code == 200
    me = (await client.get("/v1/me")).json()
    assert me["avatar"] is not None
    # Avatars get one small size, not the three post sizes.
    assert list(me["avatar"]) == ["320"]


async def test_an_avatar_shows_on_the_public_profile(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    viewer = await make_user(db_session, at=NYERI_TOWN)
    avatar = await make_ready_image(db_session, storage, owner=user, purpose=MediaPurpose.avatar)
    await (await authenticate(user)).put("/v1/me/avatar", json={"media_id": str(avatar.id)})

    profile = (await (await authenticate(viewer)).get(f"/v1/users/{user.id}")).json()

    assert profile["avatar"] is not None


async def test_clearing_an_avatar(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    avatar = await make_ready_image(db_session, storage, owner=user, purpose=MediaPurpose.avatar)
    client = await authenticate(user)
    await client.put("/v1/me/avatar", json={"media_id": str(avatar.id)})

    response = await client.put("/v1/me/avatar", json={"media_id": None})

    assert response.status_code == 200
    assert (await client.get("/v1/me")).json()["avatar"] is None


async def test_a_post_image_cannot_become_an_avatar(
    db_session: AsyncSession,
    storage: MemoryStorageProvider,
    authenticate: Authenticate,
) -> None:
    user = await make_user(db_session, at=NYERI_TOWN)
    post_image = await make_ready_image(db_session, storage, owner=user)

    response = await (await authenticate(user)).put(
        "/v1/me/avatar", json={"media_id": str(post_image.id)}
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
async def test_the_job_skips_an_asset_that_is_already_done(
    db_session: AsyncSession, storage: MemoryStorageProvider
) -> None:
    """A duplicate enqueue, or the sweep getting there first. Processing twice
    would delete the variants the first run made."""
    user = await make_user(db_session, at=NYERI_TOWN)
    asset = await make_ready_image(db_session, storage, owner=user)
    objects_before = dict(storage.objects)

    status = await process_media({"job_id": "t", "session": db_session}, str(asset.id))

    assert status == "ready"
    assert storage.objects == objects_before


async def test_the_job_handles_a_vanished_asset(db_session: AsyncSession) -> None:
    ctx = {"job_id": "t", "session": db_session}

    assert await process_media(ctx, str(uuid.uuid4())) == "missing"


async def test_the_catch_up_sweep_processes_stuck_uploads(
    db_session: AsyncSession, storage: MemoryStorageProvider
) -> None:
    """Enqueueing is best-effort; this is what makes a lost job a late image
    rather than a permanently stuck one."""
    from datetime import UTC, datetime, timedelta

    from app.core.config import get_settings

    user = await make_user(db_session, at=NYERI_TOWN)
    ticket = await media_service.create_upload(
        db_session,
        storage,
        owner_id=user.id,
        purpose=MediaPurpose.post,
        content_type="image/jpeg",
        settings=get_settings(),
    )
    assert ticket.asset.original_key is not None
    await storage.put_object(
        storage_key=ticket.asset.original_key, data=jpeg_with_gps(), content_type="image/jpeg"
    )
    asset = await media_service.mark_uploaded(db_session, storage, asset=ticket.asset)
    asset.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.flush()

    result = await catch_up_media({"job_id": "t", "session": db_session})

    assert result["processed"] == 1
    await db_session.refresh(asset)
    assert asset.status == MediaStatus.ready


async def test_abandoned_uploads_are_swept(
    db_session: AsyncSession, storage: MemoryStorageProvider
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.core.config import get_settings

    user = await make_user(db_session, at=NYERI_TOWN)
    ticket = await media_service.create_upload(
        db_session,
        storage,
        owner_id=user.id,
        purpose=MediaPurpose.post,
        content_type="image/jpeg",
        settings=get_settings(),
    )
    ticket.asset.created_at = datetime.now(UTC) - timedelta(days=3)
    await db_session.flush()

    swept = await media_service.sweep_abandoned_uploads(
        db_session, storage, settings=get_settings()
    )

    assert swept == 1
    assert await media_service.get_asset(db_session, ticket.asset.id) is None


async def test_uploads_require_authentication(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/media/uploads", json={"purpose": "post", "content_type": "image/jpeg"}
    )

    assert response.status_code == 401
