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
    assert current_revision(settings) == "0018_notification_cutoffs"
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
    assert capsys.readouterr().out.strip() == "0018_notification_cutoffs"
    assert cli.main(["check"]) == 0
    assert cli.main(["downgrade"]) == 0


def test_safe_defaults_migration_pauses_existing_queue(settings: Settings) -> None:
    upgrade_database(settings, "0015_background_controls")
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE system_state "
                    "SET state = 'RUNNING', next_apply_at = CURRENT_TIMESTAMP "
                    "WHERE id = 1"
                )
            )
    finally:
        database.close()

    upgrade_database(settings)
    migrated = create_database(settings)
    try:
        with migrated.engine.connect() as connection:
            state, next_apply_at = connection.execute(
                text("SELECT state, next_apply_at FROM system_state WHERE id = 1")
            ).one()

        assert state == "PAUSED"
        assert next_apply_at is None
    finally:
        migrated.close()

    downgrade_database(settings, "0015_background_controls")
    downgraded = create_database(settings)
    try:
        with downgraded.engine.connect() as connection:
            state = connection.execute(
                text("SELECT state FROM system_state WHERE id = 1")
            ).scalar_one()
        assert state == "PAUSED"
    finally:
        downgraded.close()


def test_notification_cutoff_migration_blocks_pending_external_history(
    settings: Settings,
) -> None:
    upgrade_database(settings, "0017_supervised_lease")
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE application_settings "
                    "SET telegram_enabled = TRUE, email_enabled = TRUE, "
                    "notification_routing = "
                    """'{"NEW_MESSAGE":["TELEGRAM","EMAIL"]}'::jsonb """
                    "WHERE id = 1"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO notifications "
                    "(deduplication_key, event_type, channel, state, payload, "
                    "scheduled_at, created_at) VALUES "
                    "('old-telegram', 'NEW_MESSAGE', 'TELEGRAM', 'PENDING', "
                    "'{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                    "('old-email', 'NEW_MESSAGE', 'EMAIL', 'PENDING', "
                    "'{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
    finally:
        database.close()

    upgrade_database(settings)
    migrated = create_database(settings)
    try:
        with migrated.engine.connect() as connection:
            result = connection.execute(
                text("SELECT state, error_code FROM notifications ORDER BY deduplication_key")
            ).all()
            rows = [(str(row.state), str(row.error_code)) for row in result]
            cutoffs = connection.execute(
                text("SELECT notification_cutoffs FROM application_settings WHERE id = 1")
            ).scalar_one()
        assert rows == [
            ("FAILED", "HISTORICAL_EVENT_SUPPRESSED"),
            ("FAILED", "HISTORICAL_EVENT_SUPPRESSED"),
        ]
        assert {"NEW_MESSAGE:TELEGRAM", "NEW_MESSAGE:EMAIL"} <= set(cutoffs)
    finally:
        migrated.close()


def test_direction_migration_preserves_existing_application(settings: Settings) -> None:
    upgrade_database(settings, "0003_queue_and_states")
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            vacancy_id = connection.execute(
                text(
                    "INSERT INTO vacancies (hh_id, title, source_url) "
                    "VALUES ('legacy-1', 'Legacy vacancy', 'https://hh.ru/vacancy/legacy-1') "
                    "RETURNING id"
                )
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO applications (vacancy_id, resume_hh_id, state) "
                    "VALUES (:vacancy_id, 'legacy-resume', 'APPLYING')"
                ),
                {"vacancy_id": vacancy_id},
            )
    finally:
        database.close()

    upgrade_database(settings)
    migrated = create_database(settings)
    try:
        with migrated.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT account.label, resume.hh_id, application.state "
                    "FROM applications AS application "
                    "JOIN hh_accounts AS account ON account.id = application.account_id "
                    "JOIN resumes AS resume ON resume.id = application.resume_id"
                )
            ).one()

        assert row == ("Imported data", "legacy-resume", "APPLYING")
        assert current_revision(settings) == "0018_notification_cutoffs"
    finally:
        migrated.close()
