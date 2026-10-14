"""Declarative base and the mixins every model reuses.

Three conventions are fixed here because changing them later means migrating every
table in the system:

* **UUID primary keys**, generated client-side, so a record has an identity before
  it is flushed and IDs are not guessable or enumerable.
* **Timezone-aware timestamps**, stored as `timestamptz` in UTC. `Africa/Nairobi`
  is a presentation concern and only appears where the brief says it should:
  event scheduling and reminders.
* **Soft deletion.** User content is marked deleted, not removed: moderation needs
  the evidence, and a retention job purges it later. `SoftDeleteMixin` carries the
  marker; filtering it out is the querying module's job, so that moderation code
  can still see what everyone else cannot.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint and index names. Without this, PostgreSQL invents names,
# Alembic autogenerate cannot match them up, and a migration that needs to drop a
# constraint has nothing to name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every model. Import this, never create a second base."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """A `uuid4` primary key, generated in Python."""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, sort_order=-100
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """Creation and last-update timestamps.

    `created_at` has **both** a Python default and a server default, on purpose.
    PostgreSQL's `now()` is the time the *transaction* started, not the time of
    the statement, so every row inserted in one transaction gets an identical
    timestamp. Newest-first ordering then falls back to the random-UUID tiebreaker
    and those rows come out in arbitrary order. Production requests are one
    transaction each, but seed scripts, imports, and Phase 8's half-million-post
    load are not — and a feed whose order depends on how the data was loaded is a
    feed nobody can reason about.

    The Python default gives each ORM insert its own wall-clock time. The server
    default stays so raw SQL and migrations still stamp rows. Found by
    `test_the_feed_is_newest_first`, which created three posts in one transaction.

    `updated_at` follows the same rule, for the same reason, since Phase 3 sorts by
    it — the inbox, the notification list and sync are all "most recently active
    first". Found by `test_a_read_notification_is_not_collapsed_into`, whose two
    notifications tied and came out in either order. A Python `onupdate` also means
    the new value is known without a round trip, so an updated row is not left
    with an expired attribute that async code cannot lazily reload.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
        sort_order=100,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        onupdate=_utc_now,
        nullable=False,
        sort_order=101,
    )


class SoftDeleteMixin:
    """Marks a row deleted without removing it."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True, sort_order=102
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


def db_enum(enum_class: type[StrEnum], *, name: str, length: int) -> Enum:
    """A closed set of values, enforced by the database.

    Rendered as VARCHAR plus a CHECK constraint, not a native PostgreSQL enum type:
    adding a value to a native enum needs `ALTER TYPE`, which is awkward to reverse,
    while changing a CHECK constraint is an ordinary migration.

    `create_constraint=True` is the part that matters, and it was missing for the
    whole of Phases 1 and 2. SQLAlchemy has not emitted the constraint by default
    since 1.4, so every status, reason and kind column accepted any string at all
    — found when a direct insert of `status = 'definitely_not_a_status'` into
    `users` succeeded. The ORM would only have noticed on the next *read* of that
    row, by failing it. Every enum column goes through this helper so the mistake
    cannot be made one column at a time.

    Values, not member names, are stored, so the database holds exactly the strings
    the API speaks.
    """
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
