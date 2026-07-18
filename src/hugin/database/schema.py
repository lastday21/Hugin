from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from hugin.core.settings import Settings
from hugin.database.engine import create_database, sqlite_url


def _alembic_config(settings: Settings) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).with_name("migrations")),
    )
    config.attributes["database_url"] = sqlite_url(settings.database_path)
    return config


def upgrade_database(settings: Settings, revision: str = "head") -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(settings), revision)


def downgrade_database(settings: Settings, revision: str = "base") -> None:
    command.downgrade(_alembic_config(settings), revision)


def check_database_schema(settings: Settings) -> None:
    command.check(_alembic_config(settings))


def current_revision(settings: Settings) -> str | None:
    if not settings.database_path.exists():
        return None

    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return context.get_current_revision()
    finally:
        database.close()
