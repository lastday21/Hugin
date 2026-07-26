from __future__ import annotations

from collections.abc import Iterator

from fastapi import Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from hugin.database.engine import Database


def database(request: Request) -> Database:
    value = getattr(request.app.state, "database", None)
    if not isinstance(value, Database):
        raise RuntimeError("База данных приложения не настроена")
    return value


def read_session(request: Request) -> Iterator[Session]:
    with database(request).sessions() as session:
        yield session


def write_session(request: Request) -> Iterator[Session]:
    with database(request).sessions.begin() as session:
        yield session


def require_session_key(
    request: Request,
    x_hugin_session: str | None = Header(default=None),
) -> None:
    expected = getattr(request.app.state, "session_key", "")
    if not expected or x_hugin_session != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Неверный ключ местного сеанса",
        )
