from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from hugin.core.settings import Settings


def sqlite_url(database_path: Path) -> URL:
    return URL.create("sqlite+pysqlite", database=str(database_path))


def _configure_sqlite(
    dbapi_connection: DBAPIConnection,
    _: ConnectionPoolEntry,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


@dataclass(frozen=True, slots=True)
class Database:
    engine: Engine
    sessions: sessionmaker[Session]

    def close(self) -> None:
        self.engine.dispose()


def create_database(settings: Settings) -> Database:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        sqlite_url(settings.database_path),
        pool_pre_ping=True,
    )
    event.listen(engine, "connect", _configure_sqlite)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Database(engine=engine, sessions=sessions)
