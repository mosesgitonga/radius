"""Alembic environment.

Two things here are not the generated default and matter:

**The database URL comes from application settings**, not from `alembic.ini`, so
there is one source of truth and no credentials in a committed file.

**GeoAlchemy2's Alembic helpers are installed.** PostGIS columns are not ordinary
columns: creating one also creates a spatial index and, on some backends,
constraints, all managed by PostGIS rather than by the table DDL. Without these
helpers, autogenerate produces migrations that try to create indexes that already
exist, or that drop the `spatial_ref_sys` table PostGIS owns. The `writer` rewrites
spatial operations into GeoAlchemy2's own, `include_object` filters out
PostGIS-managed objects, and `render_item` makes spatial types render importably
in the generated script.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the model modules is what populates `Base.metadata`. Autogenerate sees
# only what has been imported, so a model nobody imports here produces an empty
# migration and no warning at all. Every new module's models go in this list.
from app.core import idempotency as _idempotency_model  # noqa: F401
from app.core.config import get_settings
from app.core.models import Base
from app.modules.auth import models as _auth_models  # noqa: F401
from app.modules.events import models as _events_models  # noqa: F401
from app.modules.feed import models as _feed_models  # noqa: F401
from app.modules.groups import models as _groups_models  # noqa: F401
from app.modules.location import models as _location_models  # noqa: F401
from app.modules.marketplace import models as _marketplace_models  # noqa: F401
from app.modules.media import models as _media_models  # noqa: F401
from app.modules.messaging import models as _messaging_models  # noqa: F401
from app.modules.moderation import models as _moderation_models  # noqa: F401
from app.modules.notifications import models as _notifications_models  # noqa: F401
from app.modules.services import models as _services_models  # noqa: F401
from app.modules.staff import models as _staff_models  # noqa: F401
from app.modules.users import models as _users_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def _extension_owned_tables(connection: Connection) -> frozenset[str]:
    """Tables that belong to an installed extension, not to us.

    The PostGIS image's default database also gets `postgis_topology` and the tiger
    geocoder, which add dozens of tables and put their schemas on the search path.
    Autogenerate would then propose dropping all of them. PostgreSQL records which
    objects an extension owns (`pg_depend`, type `e`), so ask it.
    """
    rows = connection.execute(
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_depend d ON d.objid = c.oid "
            "AND d.classid = 'pg_class'::regclass AND d.deptype = 'e' "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')"
        )
    )
    return frozenset(row[0] for row in rows)


def _include_object_factory(extension_tables: frozenset[str]) -> Any:
    def include_object(
        obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
    ) -> bool:
        table_name = (
            name if type_ == "table" else getattr(getattr(obj, "table", None), "name", None)
        )
        if reflected and compare_to is None and table_name in extension_tables:
            return False
        # geoalchemy2 ships this helper untyped.
        return bool(alembic_helpers.include_object(obj, name, type_, reflected, compare_to))  # type: ignore[no-untyped-call]

    return include_object


def _configure_kwargs(extension_tables: frozenset[str] = frozenset()) -> dict[str, Any]:
    return {
        "target_metadata": target_metadata,
        # Detect column type and server-default changes, which autogenerate
        # ignores by default and which are a common source of drift.
        "compare_type": True,
        "compare_server_default": True,
        "include_object": _include_object_factory(extension_tables),
        "render_item": alembic_helpers.render_item,
        "process_revision_directives": alembic_helpers.writer,
        # Keep every migration's DDL in one transaction so a failure part-way
        # leaves nothing half-applied.
        "transaction_per_migration": True,
    }


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it, for review or manual apply."""
    context.configure(
        url=settings.sync_database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    extension_tables = _extension_owned_tables(connection)
    # The lookup above began a transaction. End it before Alembic starts: if one is
    # already open, Alembic treats it as the caller's and never commits, and every
    # migration would roll back, silently, when the connection closed.
    connection.commit()
    context.configure(connection=connection, **_configure_kwargs(extension_tables))
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: this process runs migrations and exits, so pooling buys
        # nothing and a lingering connection can block the next deploy step.
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
