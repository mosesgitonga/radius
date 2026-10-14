"""The image pipeline.

The brief's acceptance criterion for Phase 2 includes "uploaded images come back
without EXIF". That is a privacy requirement, not a nicety: a phone photo routinely
carries GPS coordinates precise to a few metres, and this product goes to
considerable lengths never to hold one. These tests build a photo with those
coordinates in it and prove none of it survives.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.modules.media.images import (
    ImageRejectedError,
    detect_content_type,
    has_exif,
    process_image,
    read_gps_tags,
    validate_upload,
)
from tests.factories import jpeg_with_gps, png_bytes

MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 50_000_000
SIZES = (320, 720, 1280)


def process(data: bytes, sizes: tuple[int, ...] = SIZES):  # type: ignore[no-untyped-def]
    return process_image(data, sizes=sizes, quality=80, max_bytes=MAX_BYTES, max_pixels=MAX_PIXELS)


# ---------------------------------------------------------------------------
# Metadata is removed
# ---------------------------------------------------------------------------
def test_the_fixture_really_carries_gps() -> None:
    """Without this the stripping tests below could pass on an image that never
    had anything to strip."""
    original = jpeg_with_gps()

    assert has_exif(original)
    assert len(read_gps_tags(original)) >= 4


def test_no_variant_carries_gps() -> None:
    result = process(jpeg_with_gps())

    for size, variant in result.variants.items():
        assert read_gps_tags(variant.data) == {}, size


def test_no_variant_carries_any_exif_at_all() -> None:
    """Not only GPS: camera serials, capture times, and owner names live in EXIF
    too. The pipeline writes new files from decoded pixels, so nothing survives."""
    result = process(jpeg_with_gps())

    for size, variant in result.variants.items():
        assert not has_exif(variant.data), size


def test_the_camera_model_does_not_survive_in_the_bytes() -> None:
    """Belt and braces: search the raw output for a string the input contained."""
    result = process(jpeg_with_gps())

    for variant in result.variants.values():
        assert b"TestCamera" not in variant.data
        assert b"Model X" not in variant.data


def test_variants_are_webp() -> None:
    result = process(jpeg_with_gps())

    for variant in result.variants.values():
        assert detect_content_type(variant.data) == "image/webp"
        assert variant.content_type == "image/webp"


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------
def test_each_requested_width_is_produced() -> None:
    result = process(jpeg_with_gps(width=1600, height=1200))

    assert set(result.variants) == {320, 720, 1280}
    assert result.variants[320].width == 320
    assert result.variants[1280].width == 1280


def test_aspect_ratio_is_kept() -> None:
    result = process(jpeg_with_gps(width=1600, height=1200))

    variant = result.variants[720]
    assert variant.height == 540


def test_portrait_images_size_by_their_longest_edge() -> None:
    result = process(jpeg_with_gps(width=1200, height=1600))

    assert result.variants[720].height == 720
    assert result.variants[720].width == 540


def test_small_images_are_never_upscaled() -> None:
    """A blurry 1280px render of a 400px photo wastes a user's data to look worse."""
    result = process(png_bytes(400, 300))

    # 320 is a genuine downscale; 720 and 1280 are larger than the source, so
    # they stay at the source's own size rather than being blown up.
    assert (result.variants[320].width, result.variants[320].height) == (320, 240)
    assert (result.variants[720].width, result.variants[720].height) == (400, 300)
    assert (result.variants[1280].width, result.variants[1280].height) == (400, 300)


def test_smaller_variants_are_smaller_files() -> None:
    """The reason the sizes exist: mid-range Android on expensive data."""
    result = process(jpeg_with_gps(width=1600, height=1200))

    assert len(result.variants[320].data) < len(result.variants[1280].data)


def test_the_source_dimensions_are_reported() -> None:
    result = process(jpeg_with_gps(width=1600, height=1200))

    assert (result.source_width, result.source_height) == (1600, 1200)


# ---------------------------------------------------------------------------
# Orientation and colour modes
# ---------------------------------------------------------------------------
def test_exif_orientation_is_applied_before_it_is_discarded() -> None:
    """Skipping this is why uploaded photos appear sideways: the pixels are stored
    rotated and only the orientation tag says so. Strip the tag without applying it
    and every portrait photo from a phone turns on its side."""
    image = Image.new("RGB", (800, 400), (200, 10, 10))
    exif = image.getexif()
    exif[274] = 6  # Orientation: rotate 90° clockwise to display.
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)

    result = process(buffer.getvalue(), sizes=(1280,))

    variant = result.variants[1280]
    assert variant.height > variant.width
    assert not has_exif(variant.data)


@pytest.mark.parametrize("mode", ["P", "L", "RGBA", "CMYK"])
def test_unusual_colour_modes_are_converted(mode: str) -> None:
    """WebP has no palette and no CMYK. An unconverted image saves as garbage."""
    image = Image.new(mode, (200, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG" if mode == "CMYK" else "PNG")

    result = process(buffer.getvalue(), sizes=(320,))

    with Image.open(io.BytesIO(result.variants[320].data)) as decoded:
        assert decoded.mode in ("RGB", "RGBA")


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "empty_file"),
        (b"this is not an image at all", "unsupported_format"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "unsupported_format"),
        (b"GIF89a" + b"\x00" * 100, "unsupported_format"),
        (b"%PDF-1.7 fake", "unsupported_format"),
    ],
)
def test_non_images_are_rejected(data: bytes, code: str) -> None:
    """SVG is singled out because it is a document that can carry script. GIF is
    refused because an animated avatar is a feature nobody asked for and a decoder
    surface nobody wants."""
    with pytest.raises(ImageRejectedError) as caught:
        validate_upload(data, max_bytes=MAX_BYTES, max_pixels=MAX_PIXELS)

    assert caught.value.code == code


def test_a_truncated_image_is_rejected() -> None:
    with pytest.raises(ImageRejectedError) as caught:
        validate_upload(jpeg_with_gps()[:200], max_bytes=MAX_BYTES, max_pixels=MAX_PIXELS)

    assert caught.value.code == "corrupt_image"


def test_an_oversized_file_is_rejected_before_decoding() -> None:
    with pytest.raises(ImageRejectedError) as caught:
        validate_upload(jpeg_with_gps(), max_bytes=1024, max_pixels=50_000_000)

    assert caught.value.code == "file_too_large"


def test_a_decompression_bomb_is_rejected_from_its_header() -> None:
    """A few kilobytes that expand into gigabytes of pixels. Caught from the
    dimensions before any large allocation happens."""
    with pytest.raises(ImageRejectedError) as caught:
        validate_upload(png_bytes(4000, 4000), max_bytes=12 * 1024 * 1024, max_pixels=1_000_000)

    assert caught.value.code == "too_many_pixels"


def test_the_declared_type_is_not_trusted() -> None:
    """A PNG uploaded as a JPEG is identified as what it is."""
    detected = validate_upload(
        png_bytes(), declared_content_type="image/jpeg", max_bytes=MAX_BYTES, max_pixels=MAX_PIXELS
    )

    assert detected == "image/png"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"\x00\x00\x00\x18ftypheic", "image/heic"),
        (b"\x00\x00\x00\x18ftypmif1", "image/heif"),
        (b"plain text", "application/octet-stream"),
    ],
)
def test_content_type_detection_from_magic_bytes(data: bytes, expected: str) -> None:
    assert detect_content_type(data) == expected


def test_rejection_reasons_are_safe_to_show() -> None:
    """The reason is returned to the uploader, so it must not leak internals."""
    with pytest.raises(ImageRejectedError) as caught:
        validate_upload(b"nonsense", max_bytes=MAX_BYTES, max_pixels=MAX_PIXELS)

    assert "Traceback" not in caught.value.reason
    assert "PIL" not in caught.value.reason
