from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hugin.api.dependencies import read_session, require_session_key, write_session
from hugin.database.models import HhAccountModel
from hugin.services.background_processes import BackgroundProcessService, ProcessKey

router = APIRouter(prefix="/api/processes", tags=["processes"])
ReadSession = Annotated[Session, Depends(read_session)]
WriteSession = Annotated[Session, Depends(write_session)]
SessionGuard = Annotated[None, Depends(require_session_key)]
AccountId = Annotated[int, Query(ge=1)]


class ProcessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_interval_minutes: int = Field(ge=1, le=1440, strict=True)
    status_interval_minutes: int = Field(ge=1, le=1440, strict=True)


def _service(session: Session, account_id: int) -> BackgroundProcessService:
    if session.get(HhAccountModel, account_id) is None:
        raise HTTPException(status_code=404, detail="Аккаунт hh.ru не найден")
    return BackgroundProcessService(session, account_id)


@router.get("")
def processes(session: ReadSession, account_id: AccountId = 1) -> dict[str, object]:
    return BackgroundProcessService(session, account_id).snapshot()


@router.post("/stop-all")
def stop_all(
    session: WriteSession, _guard: SessionGuard, account_id: AccountId = 1
) -> dict[str, object]:
    service = _service(session, account_id)
    service.stop_all()
    return service.snapshot()


@router.post("/synchronization/check-now")
def check_now(
    session: WriteSession, _guard: SessionGuard, account_id: AccountId = 1
) -> dict[str, object]:
    service = _service(session, account_id)
    try:
        service.request_check_now()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return service.snapshot()


@router.put("/synchronization/schedule")
def schedule(
    payload: ScheduleUpdate, session: WriteSession, _guard: SessionGuard, account_id: AccountId = 1
) -> dict[str, object]:
    service = _service(session, account_id)
    service.set_schedule(payload.message_interval_minutes, payload.status_interval_minutes)
    return service.snapshot()


@router.put("/{key}")
def update_process(
    key: ProcessKey,
    payload: ProcessUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: AccountId = 1,
) -> dict[str, object]:
    service = _service(session, account_id)
    try:
        service.set_enabled(key, payload.enabled)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return service.snapshot()
