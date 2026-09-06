from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from hugin.api.dependencies import read_session
from hugin.services.operation_trace import OperationTraceService

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.get("/journal")
def journal_window(
    request: Request,
    since: datetime,
    until: datetime,
    session: Annotated[Session, Depends(read_session)],
) -> dict[str, Any]:
    try:
        return OperationTraceService(
            session, data_dir=request.app.state.settings.data_dir
        ).journal_window(since=since, until=until)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/timeline")
def timeline(
    request: Request,
    since: datetime,
    session: Annotated[Session, Depends(read_session)],
    until: datetime | None = None,
) -> dict[str, Any]:
    try:
        return OperationTraceService(
            session, data_dir=request.app.state.settings.data_dir
        ).timeline(since=since, until=until)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/check")
def check(request: Request, session: Annotated[Session, Depends(read_session)]) -> dict[str, Any]:
    return OperationTraceService(session, data_dir=request.app.state.settings.data_dir).check()


@router.get("/applications/{application_id}")
def application_trace(
    application_id: int,
    request: Request,
    session: Annotated[Session, Depends(read_session)],
) -> dict[str, Any]:
    try:
        return OperationTraceService(
            session, data_dir=request.app.state.settings.data_dir
        ).application(application_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail="Отклик не найден") from error
