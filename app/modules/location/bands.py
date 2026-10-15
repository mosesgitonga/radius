"""Sorting by distance band in SQL, without leaking more than the band.

For content that offers "nearest first" — listings, service posts. The order is
by band, computed from the content's coarse (res-8) cell centre to the viewer's
coarse cell centre with the same boundaries as the band shown, then newest.
Ordering by exact distance would reveal ordering *within* a band, which someone
stepping their position around could use to narrow a home down to a res-9 cell
(decision 5.2).
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, or_, tuple_
from sqlalchemy.orm import InstrumentedAttribute

from app.core.errors import InvalidCursorError
from app.modules.location import cells
from app.modules.location.service import Anchor


def coarse_wkt(cell_res8: int) -> str:
    """The res-8 cell centre, as stored in a `coarse_centroid` column."""
    latitude, longitude = cells.cell_centroid(cell_res8)
    return f"SRID=4326;POINT({longitude} {latitude})"


def band_expression(coarse_centroid: InstrumentedAttribute[Any], anchor: Anchor) -> Any:
    """The band as an ordinal (0 = under 1 km). Spherical distance, to agree with
    the H3 great-circle distance used to label a band elsewhere."""
    distance = func.ST_Distance(
        coarse_centroid, func.ST_GeogFromText(coarse_wkt(anchor.cell_res8)), False
    )
    return case(
        *((distance < bound, index) for index, (bound, _bucket) in enumerate(cells.BUCKET_BOUNDS)),
        else_=len(cells.BUCKET_BOUNDS),
    )


def bucket_for(ordinal: int) -> cells.DistanceBucket:
    return cells.BUCKET_ORDER[ordinal]


def encode_cursor(bucket: int, created_at: datetime, row_id: uuid.UUID) -> str:
    payload = json.dumps([bucket, created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[int, datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        bucket, created, row_id = json.loads(base64.urlsafe_b64decode(padded))
        return int(bucket), datetime.fromisoformat(created), uuid.UUID(row_id)
    except (ValueError, TypeError) as exc:
        raise InvalidCursorError from exc


def after_cursor(
    band: Any,
    created_at: InstrumentedAttribute[datetime],
    row_id: InstrumentedAttribute[uuid.UUID],
    cursor: str,
) -> Any:
    """The keyset condition for "after this row" in (band asc, newest first) order."""
    last_bucket, last_created, last_id = decode_cursor(cursor)
    return or_(
        band > last_bucket,
        and_(band == last_bucket, tuple_(created_at, row_id) < tuple_(last_created, last_id)),
    )


def ordering(
    band: Any,
    created_at: InstrumentedAttribute[datetime],
    row_id: InstrumentedAttribute[uuid.UUID],
) -> tuple[Any, Any, Any]:
    return band.asc(), created_at.desc(), row_id.desc()
