from __future__ import annotations

from pathlib import Path

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


def test_database_enables_integrity_pragmas(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path)
    database = create_database(settings)

    try:
        with database.engine.connect() as connection:
            foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
            busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
            journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()

        assert foreign_keys == 1
        assert busy_timeout == 5000
        assert journal_mode == "wal"
    finally:
        database.close()


def test_migration_reaches_baseline(tmp_path: Path) -> None:
    settings = Settings(environment="test", data_dir=tmp_path)

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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(environment="test", data_dir=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["upgrade"]) == 0
    assert cli.main(["current"]) == 0
    assert capsys.readouterr().out.strip() == "0003_queue_and_states"
    assert cli.main(["check"]) == 0
    assert cli.main(["downgrade"]) == 0
