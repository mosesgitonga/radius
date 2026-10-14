"""Rendering media assets for responses.

Lives in the media module because every module that shows an image — feed,
profiles, and later listings and events — needs it, and media must not depend on
any of them.
"""

from __future__ import annotations

from app.core.providers.storage import StorageProvider
from app.modules.media.models import MediaAsset
from app.modules.media.schemas import ImageVariantOut
from app.modules.media.service import variant_views


def image_variants(
    asset: MediaAsset | None, storage: StorageProvider
) -> dict[str, ImageVariantOut] | None:
    """Served variants, or None for an asset that is missing or not ready.

    A not-ready asset yields None rather than an error: an image on someone
    else's post that is still processing should render as a placeholder, not
    break the whole page.
    """
    if asset is None or not asset.is_ready:
        return None
    return {
        size: ImageVariantOut(width=view.width, height=view.height, url=view.url)
        for size, view in variant_views(asset, storage).items()
    }
