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
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Uuid, func
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


class TimestampMixin:
    """Creation and last-update timestamps, both maintained by the database.

    `server_default` and `onupdate` are used rather than Python defaults so that
    rows written by migrations, seed scripts, or raw SQL are stamped correctly too.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, sort_order=100
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
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
