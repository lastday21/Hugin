from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from hugin.core.settings import Settings


def postgresql_url(settings: Settings, *, database_name: str | None = None) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=settings.database_user,
        password=settings.database_password.get_secret_value(),
        host=settings.database_host,
        port=settings.database_port,
        database=database_name or settings.database_name,
    )


@dataclass(frozen=True, slots=True)
class Database:
    engine: Engine
    sessions: sessionmaker[Session]

    def close(self) -> None:
        self.engine.dispose()


def create_database(settings: Settings) -> Database:
    engine = create_engine(
        postgresql_url(settings),
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.database_connect_timeout},
    )
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return Database(engine=engine, sessions=sessions)
