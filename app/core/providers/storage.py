"""Object storage.

The API never handles image bytes. Clients upload directly to storage with a
presigned PUT and the worker fetches from there, which keeps a 12 MB photo on a
slow Kenyan connection off the request path entirely — a mobile upload can take a
minute, and a minute of a Uvicorn worker is a minute nobody else gets served.

Two implementations behind one Protocol:

* `S3StorageProvider` — anything speaking S3: Cloudflare R2 in production, MinIO in
  development. The same code, a different endpoint.
* `MemoryStorageProvider` — keeps objects in a dict. Tests never touch the network,
  and a developer with no object store can still run the whole flow.

boto3 is synchronous. That is fine where it is used: presigning is a local HMAC
computation with no network call at all, and the worker's transfers go through
`asyncio.to_thread` so they do not block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Storage was unreachable or refused the operation."""


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    """What a client needs to upload one object itself."""

    url: str
    method: str
    headers: dict[str, str]
    storage_key: str
    expires_in_seconds: int


@runtime_checkable
class StorageProvider(Protocol):
    """What the media module needs from an object store, and nothing more."""

    name: str

    def presign_upload(
        self, *, storage_key: str, content_type: str, max_bytes: int
    ) -> PresignedUpload:
        """A URL the client can PUT one object to."""
        ...

    def public_url(self, *, storage_key: str) -> str:
        """A URL for serving a processed image."""
        ...

    async def get_object(self, *, storage_key: str) -> bytes: ...

    async def put_object(self, *, storage_key: str, data: bytes, content_type: str) -> None: ...

    async def delete_object(self, *, storage_key: str) -> None: ...

    async def object_exists(self, *, storage_key: str) -> bool: ...


def build_storage_key(*, purpose: str, owner_id: uuid.UUID, suffix: str) -> str:
    """Where an object lives.

    `purpose/owner/random.ext`. The random component matters: a predictable key
    would let somebody guess another user's object name, and while the bucket is
    private, defence in depth costs nothing here. Partitioning by owner also makes
    "delete everything belonging to this account" a prefix operation, which the
    Phase 8 purge will want.
    """
    return f"{purpose}/{owner_id}/{uuid.uuid4().hex}{suffix}"


# ---------------------------------------------------------------------------
# In-memory
# ---------------------------------------------------------------------------
@dataclass
class MemoryStorageProvider:
    """Objects in a dict, for tests and for a machine with no object store.

    `presign_upload` returns a URL nothing serves. That is deliberate: a test
    asserting on the presigned flow should check that a URL was issued and then
    put the bytes through `put_object`, rather than pretending an HTTP upload
    happened.
    """

    name: str = "memory"
    objects: dict[str, tuple[bytes, str]] = field(default_factory=dict)

    def presign_upload(
        self, *, storage_key: str, content_type: str, max_bytes: int
    ) -> PresignedUpload:
        return PresignedUpload(
            url=f"memory://upload/{storage_key}",
            method="PUT",
            headers={"Content-Type": content_type},
            storage_key=storage_key,
            expires_in_seconds=900,
        )

    def public_url(self, *, storage_key: str) -> str:
        return f"memory://object/{storage_key}"

    async def get_object(self, *, storage_key: str) -> bytes:
        try:
            return self.objects[storage_key][0]
        except KeyError as exc:
            raise StorageError(f"no such object: {storage_key}") from exc

    async def put_object(self, *, storage_key: str, data: bytes, content_type: str) -> None:
        self.objects[storage_key] = (data, content_type)

    async def delete_object(self, *, storage_key: str) -> None:
        self.objects.pop(storage_key, None)

    async def object_exists(self, *, storage_key: str) -> bool:
        return storage_key in self.objects

    def clear(self) -> None:
        self.objects.clear()


# ---------------------------------------------------------------------------
# S3-compatible
# ---------------------------------------------------------------------------
class S3StorageProvider:
    """S3, Cloudflare R2, or MinIO."""

    name = "s3"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None
        self._presign_client: Any | None = None

    @property
    def client(self) -> Any:
        """The client for the server's own operations.

        Built on first use, not at construction. Importing this module must not
        require credentials — the settings validator checks those — and a client
        built at import time would make every test that touches the media module
        depend on boto3's config resolution.
        """
        if self._client is None:
            self._client = self._build(self._settings.storage_endpoint_url)
        return self._client

    @property
    def presign_client(self) -> Any:
        """The client used to sign URLs handed to a phone.

        Separate because the host is part of a SigV4 signature. Where the server
        and its clients see storage at different addresses — Docker in development
        — a URL signed for the internal name would be unusable, and rewriting its
        host afterwards would break the signature.
        """
        endpoint = self._settings.storage_presign_endpoint_url
        if not endpoint or endpoint == self._settings.storage_endpoint_url:
            return self.client
        if self._presign_client is None:
            self._presign_client = self._build(endpoint)
        return self._presign_client

    def _build(self, endpoint_url: str) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            region_name=self._settings.storage_region,
            endpoint_url=endpoint_url or None,
            aws_access_key_id=self._settings.storage_access_key_id,
            aws_secret_access_key=(self._settings.storage_secret_access_key.get_secret_value()),
            config=Config(
                signature_version="s3v4",
                s3={
                    "addressing_style": (
                        "path" if self._settings.storage_force_path_style else "auto"
                    )
                },
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def presign_upload(
        self, *, storage_key: str, content_type: str, max_bytes: int
    ) -> PresignedUpload:
        """A presigned PUT.

        The content type is part of the signature, so a client cannot upload a
        video while claiming a JPEG — the signature simply will not match. Size
        cannot be bound into a presigned PUT, so the worker enforces it, and the
        limit is returned here only so the client can refuse early.
        """
        try:
            url = self.presign_client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._settings.storage_bucket,
                    "Key": storage_key,
                    "ContentType": content_type,
                },
                ExpiresIn=self._settings.presign_upload_ttl_seconds,
            )
        except Exception as exc:  # pragma: no cover — credential or config failure
            logger.error("presign_failed", extra={"provider": self.name})
            raise StorageError("could not presign the upload") from exc

        return PresignedUpload(
            url=str(url),
            method="PUT",
            headers={"Content-Type": content_type},
            storage_key=storage_key,
            expires_in_seconds=self._settings.presign_upload_ttl_seconds,
        )

    def public_url(self, *, storage_key: str) -> str:
        """A CDN URL where one is configured, otherwise a presigned GET.

        The CDN path is strongly preferred: a presigned URL is unique per request,
        so it defeats client and CDN caching entirely, and on expensive mobile data
        re-downloading an avatar on every screen is a real cost.
        """
        base = self._settings.storage_public_base_url.rstrip("/")
        if base:
            return f"{base}/{storage_key}"
        try:
            return str(
                self.presign_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._settings.storage_bucket, "Key": storage_key},
                    ExpiresIn=self._settings.presign_download_ttl_seconds,
                )
            )
        except Exception as exc:  # pragma: no cover
            raise StorageError("could not presign the download") from exc

    async def get_object(self, *, storage_key: str) -> bytes:
        def _get() -> bytes:
            response = self.client.get_object(Bucket=self._settings.storage_bucket, Key=storage_key)
            body: bytes = response["Body"].read()
            return body

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            raise StorageError(f"could not read {storage_key}") from exc

    async def put_object(self, *, storage_key: str, data: bytes, content_type: str) -> None:
        def _put() -> None:
            self.client.put_object(
                Bucket=self._settings.storage_bucket,
                Key=storage_key,
                Body=data,
                ContentType=content_type,
                # Processed variants are immutable — a new upload gets a new key —
                # so they can be cached hard. That is most of the bandwidth saving
                # for a user on a metered connection.
                CacheControl="public, max-age=31536000, immutable",
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            raise StorageError(f"could not write {storage_key}") from exc

    async def delete_object(self, *, storage_key: str) -> None:
        def _delete() -> None:
            self.client.delete_object(Bucket=self._settings.storage_bucket, Key=storage_key)

        try:
            await asyncio.to_thread(_delete)
        except Exception:
            # A failed delete leaves an orphan, which costs storage but breaks
            # nothing. Logged for the sweeper rather than raised at the caller.
            logger.warning("storage_delete_failed", extra={"storage_key": storage_key})

    def ensure_bucket(self) -> bool:
        """Create the bucket if it does not exist. For development only.

        Production buckets are provisioned deliberately, with their own access
        policy and lifecycle rules; an application that creates its own bucket on
        boot would quietly paper over a misconfigured environment.
        """
        bucket = self._settings.storage_bucket
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:
            self.client.create_bucket(Bucket=bucket)
            return True
        return False

    async def object_exists(self, *, storage_key: str) -> bool:
        def _head() -> bool:
            try:
                self.client.head_object(Bucket=self._settings.storage_bucket, Key=storage_key)
            except Exception:
                return False
            return True

        return await asyncio.to_thread(_head)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def build_storage_provider(settings: Settings) -> StorageProvider:
    if settings.storage_provider == "s3":
        return S3StorageProvider(settings)
    return MemoryStorageProvider()


_provider: StorageProvider | None = None


def get_storage_provider() -> StorageProvider:
    """The process-wide provider.

    A singleton because the memory implementation holds state that a request and
    the worker both need to see.
    """
    global _provider
    if _provider is None:
        from app.core.config import get_settings

        _provider = build_storage_provider(get_settings())
    return _provider


def set_storage_provider(provider: StorageProvider | None) -> None:
    """Override the provider. Used by tests; `None` restores the default."""
    global _provider
    _provider = provider
