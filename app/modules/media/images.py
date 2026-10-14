"""Image validation and processing.

Pure functions over bytes, so the hard parts can be tested without storage, a
database, or a worker.

**The privacy job.** A photo taken on a phone routinely carries GPS coordinates,
the exact capture time, the device serial, and sometimes the owner's name — all in
EXIF. This product goes to considerable lengths never to store a coordinate, and a
single un-stripped holiday snap would hand out a precise one. So processing does
not *edit* metadata; it decodes the pixels and writes an entirely new file, which
is the only way to be certain nothing came along. The original is then deleted.

**The security job.** The client's declared content type is a claim, not a fact.
Type is determined from the file's magic bytes, dimensions are checked before any
large allocation, and `Image.verify()` runs before decoding, so a crafted file
cannot turn an upload into a denial of service or worse.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Formats accepted on upload. Deliberately short: these cover Android cameras and
# every screenshot, and each is a format Pillow decodes natively.
#
# Not accepted, on purpose: SVG (it is a document that can carry script), TIFF
# (vast, and a historic source of decoder bugs), and anything animated — an
# animated avatar is a feature nobody asked for and a decoder surface nobody wants.
#
# **HEIC/HEIF is not accepted yet**, and that one is a limitation rather than a
# choice. Pillow cannot decode it without the `pillow-heif` plugin, which is not in
# the agreed dependencies. It used to be listed here, which meant a HEIC was
# accepted at presign, uploaded at the user's expense, then rejected as "could not
# be read" — the worst of both. It is refused up front now, with the supported list,
# so the client can convert before spending the user's data. Detection still
# recognises it, so a HEIC that arrives anyway gets a clear `unsupported_format`.
ALLOWED_CONTENT_TYPES: Final[frozenset[str]] = frozenset({"image/jpeg", "image/png", "image/webp"})

OUTPUT_CONTENT_TYPE: Final = "image/webp"
OUTPUT_SUFFIX: Final = ".webp"

# Magic bytes, checked instead of trusting the declared type.
_SIGNATURES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class ImageRejectedError(Exception):
    """The upload is not a usable image. The reason is safe to show a client."""

    def __init__(self, reason: str, *, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class ImageVariant:
    width: int
    height: int
    data: bytes
    content_type: str = OUTPUT_CONTENT_TYPE


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """Everything derived from one upload. The original is not among it."""

    source_width: int
    source_height: int
    detected_content_type: str
    variants: dict[int, ImageVariant]
    """Keyed by requested longest-edge width."""


def detect_content_type(data: bytes) -> str:
    """The real type, from the bytes.

    WebP and the HEIF family are container formats, so they are matched on their
    inner brand rather than a fixed prefix.
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # ISO-BMFF: a length, then 'ftyp', then a brand.
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm"):
            return "image/heic"
        if brand in (b"mif1", b"msf1"):
            return "image/heif"
    for signature, content_type in _SIGNATURES:
        if data.startswith(signature):
            return content_type
    return "application/octet-stream"


def validate_upload(
    data: bytes, *, max_bytes: int, max_pixels: int, declared_content_type: str | None = None
) -> str:
    """Check an uploaded blob and return its real content type.

    Order matters. Size is checked before anything is decoded, the type comes from
    the bytes rather than the client, and `verify()` runs before `load()` so a
    malformed file is rejected without a full decode.
    """
    if not data:
        raise ImageRejectedError("The file is empty", code="empty_file")

    if len(data) > max_bytes:
        raise ImageRejectedError(
            f"The image is larger than the {max_bytes // (1024 * 1024)} MB limit",
            code="file_too_large",
        )

    detected = detect_content_type(data)
    if detected not in ALLOWED_CONTENT_TYPES:
        raise ImageRejectedError(
            "That file is not a supported image format", code="unsupported_format"
        )

    if declared_content_type and declared_content_type != detected:
        # Worth a log line: it is occasionally an honest client bug, and
        # occasionally somebody probing what the server will accept.
        logger.info(
            "upload_content_type_mismatch",
            extra={"declared": declared_content_type, "detected": detected},
        )

    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            if width * height > max_pixels:
                # A decompression bomb: a few kilobytes that expand into
                # gigabytes of pixels. Caught from the header, before decoding.
                raise ImageRejectedError(
                    "That image has too many pixels to process", code="too_many_pixels"
                )
            # Checks structural integrity without decoding the whole image. The
            # file has to be reopened afterwards, because verify() exhausts it.
            probe.verify()
    except ImageRejectedError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageRejectedError("That image could not be read", code="corrupt_image") from exc

    return detected


def process_image(
    data: bytes,
    *,
    sizes: tuple[int, ...],
    quality: int,
    max_bytes: int,
    max_pixels: int,
    declared_content_type: str | None = None,
) -> ProcessedImage:
    """Validate, strip metadata, and render WebP variants.

    Variants are never upscaled: an image narrower than a requested size is
    emitted at its own width. Serving a blurry 1280px render of a 400px photo
    wastes a user's data to look worse.
    """
    detected = validate_upload(
        data,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
        declared_content_type=declared_content_type,
    )

    try:
        with Image.open(io.BytesIO(data)) as source:
            # Apply the EXIF orientation tag, then discard it with everything
            # else. Skipping this is why uploaded photos appear sideways: the
            # pixels are rotated and only the tag says so.
            oriented = ImageOps.exif_transpose(source) or source

            # RGB because WebP has no concept of a palette or of CMYK, and an
            # unconverted palette image saves as something unreadable.
            if oriented.mode not in ("RGB", "RGBA"):
                oriented = oriented.convert("RGBA" if "transparency" in oriented.info else "RGB")

            source_width, source_height = oriented.size
            variants: dict[int, ImageVariant] = {}
            for size in sizes:
                variants[size] = _render_variant(oriented, size, quality)
    except ImageRejectedError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageRejectedError("That image could not be processed", code="corrupt_image") from exc

    return ProcessedImage(
        source_width=source_width,
        source_height=source_height,
        detected_content_type=detected,
        variants=variants,
    )


def _render_variant(image: Image.Image, size: int, quality: int) -> ImageVariant:
    """One WebP at a given longest-edge width, carrying no metadata."""
    width, height = image.size
    longest = max(width, height)

    if longest <= size:
        resized = image.copy()
    else:
        scale = size / longest
        resized = image.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            # LANCZOS: the best quality Pillow offers for downscaling, and
            # downscaling is all this ever does. Cheaper filters show their
            # artefacts most on exactly what people photograph — text and faces.
            Image.Resampling.LANCZOS,
        )

    buffer = io.BytesIO()
    # A new file from decoded pixels. Nothing from the source's metadata can
    # survive this, which is the point: no GPS, no timestamp, no device serial.
    resized.save(buffer, format="WEBP", quality=quality, method=4)
    data = buffer.getvalue()
    variant = ImageVariant(width=resized.width, height=resized.height, data=data)
    resized.close()
    return variant


def has_exif(data: bytes) -> bool:
    """Whether an image carries EXIF. Used by tests to prove stripping works."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
            return bool(exif) or "exif" in image.info
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def read_gps_tags(data: bytes) -> dict[int, object]:
    """Any GPS EXIF block, as raw tags.

    Exists so a test can assert that coordinates present in an upload are absent
    from everything served — the one metadata leak that would matter most here.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
            # 34853 is the GPSInfo IFD pointer.
            gps = exif.get_ifd(34853)
            return dict(gps) if gps else {}
    except (UnidentifiedImageError, OSError, ValueError, KeyError):
        return {}
