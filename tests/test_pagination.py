"""Cursor pagination: encoding, and keyset querying against PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import String, Table, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateTable

from app.core.config import Settings
from app.core.errors import InvalidCursorError
from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.core.pagination import (
    CursorPage,
    PageParams,
    decode_cursor,
    encode_cursor,
    page_params,
    paginate,
)


class PaginationRow(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A throwaway table, created and dropped inside each test's transaction.

    Phase 0 has no domain models yet, but the paginator is shared by every list
    endpoint in every later phase, so it is worth testing against real SQL now
    rather than trusting it on first use.
    """

    __tablename__ = "test_pagination_rows"

    label: Mapped[str] = mapped_column(String(32))


def _create_table() -> CreateTable:
    """DDL for the throwaway table.

    `__table__` is declared as a `FromClause` on the declarative base, so it
    is narrowed here rather than at both call sites.
    """
    return CreateTable(cast("Table", PaginationRow.__table__))


@pytest.fixture
async def rows(db_session: AsyncSession) -> list[PaginationRow]:
    """Nine rows with distinct, descending timestamps."""
    await db_session.execute(_create_table())

    base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    created = [
        PaginationRow(
            label=f"row-{index:02d}",
            created_at=base - timedelta(minutes=index),
            updated_at=base - timedelta(minutes=index),
        )
        for index in range(9)
    ]
    db_session.add_all(created)
    await db_session.flush()
    return created


# ---------------------------------------------------------------------------
# Cursor encoding
# ---------------------------------------------------------------------------
def test_cursor_round_trips_a_timestamp() -> None:
    moment = datetime(2026, 3, 1, 12, 30, 45, 123456, tzinfo=UTC)
    row_id = uuid.uuid4()

    sort_value, decoded_id = decode_cursor(encode_cursor(moment, row_id))

    assert sort_value == moment
    assert decoded_id == row_id


@pytest.mark.parametrize(
    "sort_value",
    [
        datetime(2026, 1, 1, tzinfo=UTC),
        "alphabetical",
        42,
        1500.5,
        uuid.UUID("11111111-2222-3333-4444-555555555555"),
    ],
)
def test_cursor_round_trips_each_supported_sort_type(sort_value: object) -> None:
    row_id = uuid.uuid4()

    decoded, decoded_id = decode_cursor(encode_cursor(sort_value, row_id))

    assert decoded == sort_value
    assert decoded_id == row_id


def test_cursor_is_url_safe_and_unpadded() -> None:
    """Cursors travel as query parameters; padding and '+' would need escaping."""
    cursor = encode_cursor(datetime(2026, 3, 1, tzinfo=UTC), uuid.uuid4())

    assert "=" not in cursor
    assert "+" not in cursor
    assert "/" not in cursor


@pytest.mark.parametrize(
    "bad",
    [
        "not-base64-!!",
        "eyJub3RfYV9jdXJzb3IiOiB0cnVlfQ",  # valid base64 JSON, wrong shape
        "e30",  # "{}"
        "W10",  # "[]"
        "",
    ],
)
def test_malformed_cursors_are_rejected(bad: str) -> None:
    """A bad cursor is a 400 with a specific code, not a 500.

    Clients retry with stale cursors after upgrades; they need to be able to tell
    "start again from the top" from "the server is broken".
    """
    with pytest.raises(InvalidCursorError):
        decode_cursor(bad)


def test_cursor_with_an_unknown_sort_kind_is_rejected() -> None:
    import base64
    import json

    forged = base64.urlsafe_b64encode(
        json.dumps({"k": "pickle", "v": "x", "i": str(uuid.uuid4())}).encode()
    ).decode()

    with pytest.raises(InvalidCursorError):
        decode_cursor(forged)


def test_unsupported_sort_types_fail_loudly_at_encode_time() -> None:
    with pytest.raises(TypeError):
        encode_cursor({"not": "scalar"}, uuid.uuid4())


# ---------------------------------------------------------------------------
# Page parameters
# ---------------------------------------------------------------------------
def test_page_params_default_to_the_configured_limit(settings: Settings) -> None:
    params = page_params(limit=None, cursor=None, settings=settings)

    assert params.limit == settings.pagination_default_limit
    assert params.cursor is None


def test_oversized_limits_are_clamped_not_rejected(settings: Settings) -> None:
    """A client asking for too much gets a sane page rather than an error it
    cannot interpret — and the cap still protects the server and the user's
    data bundle."""
    params = page_params(limit=10_000, cursor=None, settings=settings)

    assert params.limit == settings.pagination_max_limit


# ---------------------------------------------------------------------------
# Keyset querying
# ---------------------------------------------------------------------------
async def test_first_page_returns_newest_first(
    db_session: AsyncSession, rows: list[PaginationRow]
) -> None:
    items, next_cursor = await paginate(
        db_session,
        select(PaginationRow),
        sort_column=PaginationRow.created_at,
        id_column=PaginationRow.id,
        params=PageParams(limit=4, cursor=None),
    )

    assert [row.label for row in items] == ["row-00", "row-01", "row-02", "row-03"]
    assert next_cursor is not None


async def test_pages_cover_every_row_exactly_once(
    db_session: AsyncSession, rows: list[PaginationRow]
) -> None:
    seen: list[str] = []
    cursor: str | None = None

    for _ in range(10):  # bounded: a cursor bug must not spin forever
        items, cursor = await paginate(
            db_session,
            select(PaginationRow),
            sort_column=PaginationRow.created_at,
            id_column=PaginationRow.id,
            params=PageParams(limit=4, cursor=cursor),
        )
        seen.extend(row.label for row in items)
        if cursor is None:
            break

    assert cursor is None
    assert seen == sorted(seen, reverse=True)
    assert len(seen) == len(set(seen)) == 9


async def test_last_page_reports_no_next_cursor(
    db_session: AsyncSession, rows: list[PaginationRow]
) -> None:
    """`next_cursor: null` is how a client knows to stop; an exhausted page that
    still returned a cursor would make the app poll forever."""
    items, next_cursor = await paginate(
        db_session,
        select(PaginationRow),
        sort_column=PaginationRow.created_at,
        id_column=PaginationRow.id,
        params=PageParams(limit=20, cursor=None),
    )

    assert len(items) == 9
    assert next_cursor is None


async def test_page_exactly_the_size_of_the_remainder_ends_cleanly(
    db_session: AsyncSession, rows: list[PaginationRow]
) -> None:
    """The off-by-one case: nine rows in pages of three.

    The third page is full, so a naive implementation hands back a cursor and the
    client fetches an empty fourth page.
    """
    cursor: str | None = None
    pages = 0

    for _ in range(5):
        _, cursor = await paginate(
            db_session,
            select(PaginationRow),
            sort_column=PaginationRow.created_at,
            id_column=PaginationRow.id,
            params=PageParams(limit=3, cursor=cursor),
        )
        pages += 1
        if cursor is None:
            break

    assert pages == 3


async def test_rows_sharing_a_timestamp_are_not_duplicated_or_skipped(
    db_session: AsyncSession,
) -> None:
    """The ID tiebreaker exists for this case.

    In a neighbourhood feed several posts really can land in the same instant, and
    ordering by timestamp alone makes their order undefined between queries — so
    a row appears on both pages, or on neither.
    """
    await db_session.execute(_create_table())
    moment = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    db_session.add_all(
        [
            PaginationRow(label=f"same-{index}", created_at=moment, updated_at=moment)
            for index in range(6)
        ]
    )
    await db_session.flush()

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(6):
        items, cursor = await paginate(
            db_session,
            select(PaginationRow),
            sort_column=PaginationRow.created_at,
            id_column=PaginationRow.id,
            params=PageParams(limit=2, cursor=cursor),
        )
        seen.extend(row.label for row in items)
        if cursor is None:
            break

    assert sorted(seen) == [f"same-{index}" for index in range(6)]


async def test_ascending_order_is_supported(
    db_session: AsyncSession, rows: list[PaginationRow]
) -> None:
    """Needed for upcoming events and for message history replay."""
    items, _ = await paginate(
        db_session,
        select(PaginationRow),
        sort_column=PaginationRow.created_at,
        id_column=PaginationRow.id,
        params=PageParams(limit=3, cursor=None),
        descending=False,
    )

    assert [row.label for row in items] == ["row-08", "row-07", "row-06"]


async def test_existing_filters_are_preserved(
    db_session: AsyncSession, rows: list[PaginationRow]
) -> None:
    """The paginator adds to a statement; it must never widen it.

    Every real query arrives already filtered by radius, blocking, and visibility.
    If paging dropped a filter, it would leak content.
    """
    statement = select(PaginationRow).where(PaginationRow.label.in_(["row-01", "row-05"]))

    items, next_cursor = await paginate(
        db_session,
        statement,
        sort_column=PaginationRow.created_at,
        id_column=PaginationRow.id,
        params=PageParams(limit=10, cursor=None),
    )

    assert [row.label for row in items] == ["row-01", "row-05"]
    assert next_cursor is None


async def test_invalid_cursor_from_a_client_raises_invalid_cursor(
    db_session: AsyncSession, rows: list[PaginationRow]
) -> None:
    with pytest.raises(InvalidCursorError):
        await paginate(
            db_session,
            select(PaginationRow),
            sort_column=PaginationRow.created_at,
            id_column=PaginationRow.id,
            params=PageParams(limit=4, cursor="rubbish"),
        )


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------
def test_cursor_page_serialises_to_the_agreed_shape() -> None:
    page = CursorPage[str](items=["a", "b"], next_cursor="abc")

    assert page.model_dump() == {"items": ["a", "b"], "next_cursor": "abc"}


def test_cursor_page_defaults_to_no_next_cursor() -> None:
    page = CursorPage[str](items=[])

    assert page.model_dump() == {"items": [], "next_cursor": None}
