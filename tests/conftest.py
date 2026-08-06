from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

from hugin.core.settings import Settings
from hugin.database import postgresql_url, upgrade_database


def _drop_test_database(admin: Engine, database_name: str) -> None:
    with admin.connect() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :database_name AND pid <> pg_backend_pid()"
            ),
            {"database_name": database_name},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))


@pytest.fixture(scope="session")
def postgres_admin() -> Iterator[Engine]:
    base = Settings(environment="test")
    admin = create_engine(
        postgresql_url(base, database_name="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": base.database_connect_timeout},
    )

    try:
        yield admin
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def migrated_database_template(postgres_admin: Engine) -> Iterator[str]:
    base = Settings(environment="test")
    database_name = f"hugin_test_template_{uuid4().hex}"

    try:
        with postgres_admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        upgrade_database(base.model_copy(update={"database_name": database_name}))
        yield database_name
    finally:
        _drop_test_database(postgres_admin, database_name)


@pytest.fixture
def settings(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    postgres_admin: Engine,
    migrated_database_template: str,
) -> Iterator[Settings]:
    base = Settings(environment="test")
    database_name = f"hugin_test_{uuid4().hex}"
    empty_database = request.node.get_closest_marker("empty_database") is not None

    try:
        template_clause = "" if empty_database else f' TEMPLATE "{migrated_database_template}"'
        with postgres_admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"{template_clause}'))
        yield base.model_copy(
            update={
                "database_name": database_name,
                "data_dir": tmp_path / "data",
            }
        )
    finally:
        _drop_test_database(postgres_admin, database_name)
