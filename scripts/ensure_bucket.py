"""Create the development bucket.

    uv run python -m scripts.ensure_bucket

Run by the development entrypoint. Refuses outside local and test: production
buckets are provisioned on purpose, with their own policies, and an application
that silently creates one on boot hides a misconfigured environment.
"""

from __future__ import annotations

import logging
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.providers.storage import S3StorageProvider

logger = logging.getLogger("scripts.ensure_bucket")


def main() -> int:
    settings = get_settings()
    configure_logging(settings, service="ensure_bucket")

    if settings.storage_provider != "s3":
        logger.info("ensure_bucket_skipped", extra={"reason": "storage provider is not s3"})
        return 0
    if settings.app_env not in ("local", "test"):
        logger.error("ensure_bucket_refused", extra={"environment": settings.app_env})
        return 1

    try:
        created = S3StorageProvider(settings).ensure_bucket()
    except Exception:
        logger.exception("ensure_bucket_failed")
        return 1
    logger.info("ensure_bucket_done", extra={"bucket_created": created})
    return 0


if __name__ == "__main__":
    sys.exit(main())
