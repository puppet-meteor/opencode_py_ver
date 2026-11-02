"""Alembic migration environment (R1-S2).

Migrations run on a SYNC sqlite engine (r1-slices.md §4 #6) while the runtime
uses the async aiosqlite engine from ``opencode.storage.open_database``;
SQLite is compatible at the file level, so one database file can be migrated
synchronously and then served asynchronously (verified on Windows paths by
tests/integration/test_migrations.py). The URL resolves from
``-x db_path=<database file>`` first, then the ``sqlalchemy.url`` key in
``alembic.ini``.
"""

from __future__ import annotations

from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

config = context.config


def _database_url() -> str:
    """Resolve the migration URL: ``-x db_path`` first, then ``sqlalchemy.url``."""
    x_arguments = context.get_x_argument(as_dictionary=True)
    db_path = x_arguments.get("db_path")
    if db_path:
        return f"sqlite:///{Path(db_path).as_posix()}"
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("no database URL: pass -x db_path=<file> or set sqlalchemy.url in alembic.ini")
    return url


def run_migrations_offline() -> None:
    """Emit SQL without a live database connection (``alembic upgrade --sql``)."""
    context.configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live sync engine (the runtime stays async)."""
    engine = create_engine(_database_url())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
