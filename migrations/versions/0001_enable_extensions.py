"""Enable the PostGIS and pg_trgm extensions

PostGIS backs every location query in the product: content carries a
`geography(Point, 4326)` column and proximity is answered with `ST_DWithin`
against a GiST index. `pg_trgm` backs fuzzy name and category search for groups,
listings, and service providers.

Both are enabled by migration rather than left to the PostGIS image's init
scripts, so that the schema is reproducible on any PostgreSQL 16 — a managed
instance, a CI service container, a developer's existing local server — and so
that "migrations apply cleanly on an empty database" is literally true.

Creating an extension needs sufficient privilege: superuser, or a role with
`CREATE` on the database where the extension is trusted. Managed providers
generally grant this (`rds_superuser` on AWS, `cloudsqlsuperuser` on GCP). If
this migration fails on permissions, a database administrator must run the two
`CREATE EXTENSION` statements once, after which this migration is a no-op.

Revision ID: 0001
Revises:
Created: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS so this is safe against a database where the image's init
    # scripts, or an administrator, already created them.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def _other_extensions_need(extension: str) -> bool:
    """Whether another *extension* depends on this one.

    The PostGIS image's own init script installs `postgis_topology` and the tiger
    geocoder into its default database, on top of PostGIS. Those were not created
    by this migration, and neither, then, was the PostGIS they rest on: it was there
    before us, so a downgrade leaves it there.
    """
    bind = op.get_bind()
    count = bind.execute(
        sa.text(
            "SELECT count(*) FROM pg_depend d "
            "JOIN pg_extension dependent ON d.objid = dependent.oid "
            "JOIN pg_extension base ON d.refobjid = base.oid "
            "WHERE d.classid = 'pg_extension'::regclass "
            "AND d.refclassid = 'pg_extension'::regclass "
            "AND base.extname = :name"
        ),
        {"name": extension},
    ).scalar()
    return bool(count)


def downgrade() -> None:
    # No CASCADE: dropping PostGIS with CASCADE would silently take every
    # geography column with it. If a *table* still depends on these extensions,
    # failing loudly is the correct outcome — downgrade the migrations that
    # created those tables first. Another extension depending on one is different:
    # it means the extension predates us (see `_other_extensions_need`).
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    if not _other_extensions_need("postgis"):
        op.execute("DROP EXTENSION IF EXISTS postgis")
