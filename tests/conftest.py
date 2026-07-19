from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from hugin.core.settings import Settings
from hugin.database import postgresql_url


@pytest.fixture
def settings() -> Iterator[Settings]:
    base = Settings(environment="test")
    database_name = f"hugin_test_{uuid4().hex}"
    admin = create_engine(
        postgresql_url(base, database_name="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": base.database_connect_timeout},
    )

    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield base.model_copy(update={"database_name": database_name})
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin.dispose()
