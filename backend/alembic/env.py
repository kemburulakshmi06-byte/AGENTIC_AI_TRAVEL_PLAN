from __future__ import annotations

import logging.config
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.database.base import Base  # noqa: E402
import app.database.models  # noqa: E402, F401

config = context.config
if config.config_file_name is not None:
    logging.config.fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> URL:
    configured_url = get_settings().DATABASE_URL
    if configured_url is None:
        raise RuntimeError("DATABASE_URL must be configured to run Alembic.")
    url = make_url(configured_url.get_secret_value())
    if url.drivername in {"postgres", "postgresql", "postgresql+psycopg2"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use PostgreSQL with Psycopg 3.")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url().render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable: Engine = create_engine(get_database_url(), poolclass=NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
