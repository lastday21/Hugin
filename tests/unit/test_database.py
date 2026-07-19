from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from hugin.core.settings import Settings
from hugin.database import (
    check_database_schema,
    cli,
    create_database,
    current_revision,
    downgrade_database,
    upgrade_database,
)

pytestmark = pytest.mark.integration


def test_database_uses_postgresql(settings: Settings) -> None:
    database = create_database(settings)

    try:
        with database.engine.connect() as connection:
            version = connection.execute(text("SHOW server_version_num")).scalar_one()

        assert database.engine.dialect.name == "postgresql"
        assert int(version) >= 180000
    finally:
        database.close()


def test_migration_reaches_baseline(settings: Settings) -> None:
    assert current_revision(settings) is None

    upgrade_database(settings, "0001_baseline")

    database = create_database(settings)
    try:
        assert "alembic_version" in inspect(database.engine).get_table_names()
        assert current_revision(settings) == "0001_baseline"
    finally:
        database.close()

    upgrade_database(settings)
    assert current_revision(settings) == "0003_queue_and_states"
    check_database_schema(settings)

    downgrade_database(settings)
    assert current_revision(settings) is None


def test_database_cli_manages_schema(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["upgrade"]) == 0
    assert cli.main(["current"]) == 0
    assert capsys.readouterr().out.strip() == "0003_queue_and_states"
    assert cli.main(["check"]) == 0
    assert cli.main(["downgrade"]) == 0
