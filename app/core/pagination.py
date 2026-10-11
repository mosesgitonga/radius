"""Cursor pagination.

Every list endpoint returns `{"items": [...], "next_cursor": "..." | null}` and
pages by keyset, not by offset. Two reasons, both of which matter on this product:

* `OFFSET 5000` makes PostgreSQL walk and discard 5000 rows. Keyset pagination
  reads only the page, so the hundredth page of a feed costs what the first does.
* An offset shifts under you. New posts arrive constantly in a neighbourhood
  feed, so offset paging shows duplicates and silently skips items. A keyset
  cursor names the last row seen, so the next page continues from exactly there.

The cursor is opaque base64, not signed. It does not need to be: it only
positions a query whose filters and authorisation are applied server-side
regardless, so the worst a tampered cursor can do is show its sender a page of
rows they were already entitled to — or be rejected as malformed.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.config import Settings, get_settings
from app.core.errors import InvalidCursorError

_SortKind = Literal["datetime", "str", "int", "float", "uuid"]


class CursorPage[ItemT](BaseModel):
    """The one list-response shape used across the whole API."""

    items: list[ItemT]
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Pass as the `cursor` query parameter to fetch the next page. "
            "Null means this is the last page."
        ),
    )


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int
    cursor: str | None


def page_params(
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Items per page. Defaults to the server default, capped at the maximum.",
    ),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous response's `next_cursor`."
    ),
    settings: Settings = Depends(get_settings),
) -> PageParams:
    """Dependency supplying validated paging parameters.

    An oversized `limit` is clamped rather than rejected: a client asking for too
    much should get a sane page, not an error it cannot interpret, and the cap
    still protects the server and the user's data bundle.
    """
    effective = settings.pagination_default_limit if limit is None else limit
    return PageParams(limit=min(effective, settings.pagination_max_limit), cursor=cursor)


# ---------------------------------------------------------------------------
# Cursor encoding
# ---------------------------------------------------------------------------
def _encode_sort_value(value: Any) -> tuple[_SortKind, Any]:
    match value:
        case datetime():
            return "datetime", value.isoformat()
        case bool():  # before int: bool is an int subclass
            return "int", int(value)
        case uuid.UUID():
            return "uuid", str(value)
        case int():
            return "int", value
        case float():
            return "float", value
        case str():
            return "str", value
        case _:
            raise TypeError(f"cursors cannot carry {type(value).__name__} sort values")


def _decode_sort_value(kind: _SortKind, raw: Any) -> Any:
    try:
        match kind:
            case "datetime":
                return datetime.fromisoformat(raw)
            case "uuid":
                return uuid.UUID(raw)
            case "int":
                return int(raw)
            case "float":
                return float(raw)
            case "str":
                if not isinstance(raw, str):
                    raise ValueError("expected a string")
                return raw
    except (TypeError, ValueError) as exc:
        raise InvalidCursorError from exc


def encode_cursor(sort_value: Any, row_id: uuid.UUID) -> str:
    """Pack the last row's sort key and ID into an opaque string."""
    kind, encoded = _encode_sort_value(sort_value)
    payload = json.dumps({"k": kind, "v": encoded, "i": str(row_id)}, separators=(",", ":"))
    # URL-safe and unpadded, so the cursor survives being a query parameter.
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[Any, uuid.UUID]:
    """Unpack a cursor, or raise `InvalidCursorError`."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidCursorError from exc

    if not isinstance(payload, dict) or {"k", "v", "i"} - payload.keys():
        raise InvalidCursorError
    kind = payload["k"]
    if kind not in ("datetime", "str", "int", "float", "uuid"):
        raise InvalidCursorError

    sort_value = _decode_sort_value(kind, payload["v"])
    try:
        row_id = uuid.UUID(str(payload["i"]))
    except ValueError as exc:
        raise InvalidCursorError from exc
    return sort_value, row_id


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------
async def paginate[RowT](
    session: AsyncSession,
    statement: Select[RowT],
    *,
    sort_column: InstrumentedAttribute[Any],
    id_column: InstrumentedAttribute[uuid.UUID],
    params: PageParams,
    descending: bool = True,
) -> tuple[list[RowT], str | None]:
    """Run a keyset-paginated query and return the rows plus the next cursor.

    `sort_column` is the ordering key (usually `created_at`) and `id_column` is
    the tiebreaker that makes the order total — without it, rows sharing a
    timestamp can be returned twice or skipped. The filter is a row comparison,
    `(sort, id) < (:sort, :id)`, which PostgreSQL can satisfy from a composite
    index on the same two columns, so remember to create one.
    """
    if params.cursor:
        sort_value, row_id = decode_cursor(params.cursor)
        keyset = tuple_(sort_column, id_column)
        anchor = tuple_(sort_value, row_id)
        statement = statement.where(keyset < anchor if descending else keyset > anchor)

    ordering = (
        (sort_column.desc(), id_column.desc())
        if descending
        else (sort_column.asc(), id_column.asc())
    )
    # One extra row tells us whether another page exists, with no second query
    # and no COUNT(*) over the whole set.
    statement = statement.order_by(*ordering).limit(params.limit + 1)

    rows = list((await session.execute(statement)).scalars().all())

    if len(rows) <= params.limit:
        return rows, None

    page = rows[: params.limit]
    last = page[-1]
    return page, encode_cursor(getattr(last, sort_column.key), getattr(last, id_column.key))
