"""Location models: seeded areas, and the density table the radius engine reads.

Note what is *not* here: there is no table of user locations. A user's home cell
lives on the user row and is overwritten on change, and explore-mode locations
never leave Redis. Keeping no location history is a deliberate data-protection
choice, not an oversight — see decision 1.1 and `docs/DATA_MAP.md`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover
    pass


class AreaKind:
    """The kinds of area that can be seeded.

    A plain string column with these as the known values, rather than a database
    enum: the brief requires new towns to work from configuration and seed data
    alone, and a database enum would mean a migration before Nanyuki could use a
    kind Nyeri never needed.
    """

    ESTATE = "estate"
    WARD = "ward"
    TOWN = "town"
    SUBURB = "suburb"
    COUNTY = "county"
    LANDMARK = "landmark"
    CAMPUS = "campus"

    ALL = (ESTATE, WARD, TOWN, SUBURB, COUNTY, LANDMARK, CAMPUS)


class Area(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An administrator-defined named place, used to label approximate locations.

    These are the human-readable half of the privacy model. A user's position is
    never shown; instead the coarse H3 cell that satisfies k-anonymity is resolved
    to the name of the area containing it — "Kamakwa", "Ruring'u" — or falls back
    to something generic like "near Nyeri town".

    Seeded, never user-created: `scripts/seed_areas.py` loads GeoJSON from
    `seeds/areas/`. Nothing about Nyeri is hardcoded in the application; adding a
    town is a seed file.
    """

    __tablename__ = "areas"

    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32), index=True)

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    """Areas nest: an estate inside a ward inside a town. The label falls back up
    this chain when the finest area is too small to name safely."""

    town: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    county: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    boundary: Mapped[object] = mapped_column(
        Geography(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True)
    )
    """Always a MultiPolygon, even for a single-ring estate. One type means
    queries never have to branch, and real administrative boundaries are
    frequently discontiguous anyway."""

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    """Retiring an area keeps old references resolvable while removing it from new
    labelling, which a delete would not."""

    parent: Mapped[Area | None] = relationship(remote_side="Area.id", lazy="raise")

    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="area_parent_not_self"),
        Index("ix_areas_name_trgm", "name", postgresql_using="gin",
              postgresql_ops={"name": "gin_trgm_ops"}),
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Area {self.name} ({self.kind})>"


class CellActivity(Base):
    """Per-cell population counts, refreshed by a worker every fifteen minutes.

    This is what makes the radius adaptive. Counting active users live, per
    request, would mean a aggregate query over the whole users table on every feed
    load; instead the worker precomputes it and the engine reads a handful of rows.

    Keyed by `(resolution, cell)` rather than one table per resolution, because
    two different features need different resolutions: the radius engine walks
    res-8 rings, while k-anonymity needs registered counts at res 8, 7, and 6 to
    decide how coarse an area label has to be.
    """

    __tablename__ = "cell_activity"

    resolution: Mapped[int] = mapped_column(Integer, primary_key=True)

    cell: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    """H3 index as a 64-bit integer. Stored as BIGINT rather than its hex string:
    it is half the size, compares faster, and is what H3 natively is."""

    active_user_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Users whose home is in this cell and who opened the app inside the active
    window. Explore-mode visitors are excluded by design — see decision 1.1."""

    registered_user_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Everyone whose home is in this cell, active or not. Used for k-anonymity,
    where what matters is how many people a label could refer to."""

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    """Staleness is visible, so the engine can tell "nobody is here" from
    "the refresh has not run". Phase 8 alerts on it."""

    __table_args__ = (
        CheckConstraint("active_user_count >= 0", name="cell_activity_active_non_negative"),
        CheckConstraint(
            "registered_user_count >= active_user_count",
            name="cell_activity_active_within_registered",
        ),
        # The engine reads many cells at one resolution at a time.
        Index("ix_cell_activity_resolution_active", "resolution", "active_user_count"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<CellActivity r{self.resolution}/{self.cell} active={self.active_user_count}>"
