"""Alembic environment.

Migrations are hand-written SQL-first scripts (row-level security, grants and triggers
are not something autogenerate can express), so no ``target_metadata`` is used.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from pharmasafe_platform.settings import get_settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# Tests and tooling pass the URL in-memory; otherwise use the owner URL from settings.
url: str = config.attributes.get("database_url") or get_settings().migration_database_url


def run_migrations_offline() -> None:
    context.configure(url=url, literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
