"""Alembic environment. Миграции пишутся явным SQL (DDL из DATABASE_DESIGN.md), без autogenerate."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool, text

config = context.config


def _url() -> str:
    url = os.environ.get("AICD_MIGRATOR_DATABASE_URL")
    if not url:
        path = os.environ.get("AICD_MIGRATOR_DATABASE_URL_FILE")
        if path:
            with open(path, encoding="utf-8") as fh:
                url = fh.read().strip()
    if not url:
        raise RuntimeError("AICD_MIGRATOR_DATABASE_URL(_FILE) не задан")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def run_migrations_offline() -> None:
    """`alembic upgrade --sql` — dry-run: печатает SQL без подключения (NFR-REL-04)."""
    context.configure(url=_url(), literal_binds=True, version_table_schema="aicd")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text("SET search_path = aicd"))
        connection.execute(text("SET lock_timeout = '10s'"))
        connection.commit()
        context.configure(
            connection=connection,
            version_table_schema="aicd",
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
