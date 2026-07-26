from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hugin.api.dependencies import read_session, require_session_key, write_session
from hugin.domain.content import AnswerSource
from hugin.services.queue import QueueService
from hugin.services.screening_forms import ScreeningDraftService
from hugin.services.ui_workspace import UiWorkspaceService


class DirectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    is_active: bool
    queued: int
    rejected: int


class IncidentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    severity: str
    message: str
    created_at: datetime


class DashboardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_label: str
    system_state: str
    next_apply_at: datetime | None
    daily_limit: int
    delay_min_seconds: int
    delay_max_seconds: int
    applied_today: int
    remaining_today: int
    task_counts: dict[str, int]
    pending_forms: int
    ready_letters: int
    rejected_vacancies: int
    new_messages: int
    invitations: int
    directions: tuple[DirectionResponse, ...]
    incidents: tuple[IncidentResponse, ...]


class QueueItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: int
    vacancy_id: str
    title: str
    company: str
    region: str
    source_url: str
    resume_title: str
    direction: str
    state: str
    priority: float
    scheduled_at: datetime
    last_error: str | None
    letter_state: str | None
    form_state: str | None


class RejectedVacancyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    vacancy_id: str
    title: str
    company: str
    region: str
    source_url: str
    direction: str
    score: float | None
    reasons: tuple[str, ...]


class FormQuestionResponse(BaseModel):
    field_key: str
    question: str
    field_type: str
    is_required: bool
    options: tuple[str, ...]
    answer: str | None
    source: AnswerSource | None


class FormDraftResponse(BaseModel):
    form_id: int
    application_id: int
    vacancy_id: str
    vacancy_title: str
    company: str
    source_url: str
    resume_title: str
    state: str
    answered_count: int
    unanswered_count: int
    questions: tuple[FormQuestionResponse, ...]


class VacancyQuestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    text: str
    answer: str | None
    source: str | None
    required: bool


class VacancyEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_type: str
    created_at: datetime
    details: str


class VacancyCardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    vacancy_id: str
    title: str
    company: str
    source_url: str
    region: str
    address: str
    salary: str
    employment: str
    work_format: str
    experience: str
    skills: tuple[str, ...]
    description: str
    direction: str
    state: str
    score: float | None
    reasons: tuple[str, ...]
    discoveries: tuple[str, ...]
    cover_letter: str | None
    form_state: str | None
    questions: tuple[VacancyQuestionResponse, ...]
    events: tuple[VacancyEventResponse, ...]


class QueueControlResponse(BaseModel):
    state: str
    updated_at: datetime


class QueueSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_limit: int = Field(ge=25, strict=True)
    delay_min_seconds: int = Field(ge=0, strict=True)
    delay_max_seconds: int = Field(ge=0, strict=True)


class QueueSettingsResponse(BaseModel):
    daily_limit: int
    delay_min_seconds: int
    delay_max_seconds: int


class SessionResponse(BaseModel):
    key: str


router = APIRouter(prefix="/api", tags=["workspace"])
ReadSession = Annotated[Session, Depends(read_session)]
WriteSession = Annotated[Session, Depends(write_session)]
SessionGuard = Annotated[None, Depends(require_session_key)]


@router.get("/session", response_model=SessionResponse)
def session_key(request: Request) -> SessionResponse:
    return SessionResponse(key=str(request.app.state.session_key))


@router.get("/dashboard", response_model=DashboardResponse)
def dashboard(
    session: ReadSession,
    account_id: int = Query(default=1, ge=1),
) -> DashboardResponse:
    try:
        return DashboardResponse.model_validate(UiWorkspaceService(session).dashboard(account_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/queue", response_model=tuple[QueueItemResponse, ...])
def queue(
    session: ReadSession,
    account_id: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
) -> tuple[QueueItemResponse, ...]:
    try:
        return tuple(
            QueueItemResponse.model_validate(item)
            for item in UiWorkspaceService(session).queue(account_id, limit)
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/forms", response_model=tuple[FormDraftResponse, ...])
def forms(
    session: ReadSession,
    account_id: int = Query(default=1, ge=1),
) -> tuple[FormDraftResponse, ...]:
    drafts = ScreeningDraftService(session).list_pending(account_id)
    return tuple(
        FormDraftResponse(
            form_id=draft.form_id,
            application_id=draft.application_id,
            vacancy_id=draft.vacancy_id,
            vacancy_title=draft.vacancy_title,
            company=draft.company,
            source_url=draft.source_url,
            resume_title=draft.resume_title,
            state=draft.state.value,
            answered_count=len(draft.answers),
            unanswered_count=draft.unanswered_count,
            questions=tuple(
                FormQuestionResponse(**asdict(question)) for question in draft.questions
            ),
        )
        for draft in drafts
    )


@router.get("/rejected", response_model=tuple[RejectedVacancyResponse, ...])
def rejected(
    session: ReadSession,
    account_id: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
) -> tuple[RejectedVacancyResponse, ...]:
    try:
        return tuple(
            RejectedVacancyResponse.model_validate(item)
            for item in UiWorkspaceService(session).rejected(account_id, limit)
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/vacancies/{vacancy_id}", response_model=VacancyCardResponse)
def vacancy(
    vacancy_id: str,
    session: ReadSession,
    account_id: int = Query(default=1, ge=1),
) -> VacancyCardResponse:
    try:
        return VacancyCardResponse.model_validate(
            UiWorkspaceService(session).vacancy(account_id, vacancy_id)
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/queue/pause", response_model=QueueControlResponse)
def pause_queue(
    session: WriteSession,
    _guard: SessionGuard,
) -> QueueControlResponse:
    try:
        state = QueueService(session).pause()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return QueueControlResponse(state=state.state.value, updated_at=state.updated_at)


@router.post("/queue/resume", response_model=QueueControlResponse)
def resume_queue(
    session: WriteSession,
    _guard: SessionGuard,
) -> QueueControlResponse:
    try:
        state = QueueService(session).resume()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return QueueControlResponse(state=state.state.value, updated_at=state.updated_at)


@router.put("/queue/settings", response_model=QueueSettingsResponse)
def update_queue_settings(
    values: QueueSettingsUpdate,
    session: WriteSession,
    _guard: SessionGuard,
) -> QueueSettingsResponse:
    if values.delay_max_seconds < values.delay_min_seconds:
        raise HTTPException(
            status_code=422,
            detail="Пауза «до» не может быть меньше паузы «от»",
        )
    queue = QueueService(session)
    current = queue.policy()
    try:
        saved = queue.configure(
            timezone_name=current.timezone_name,
            daily_limit=values.daily_limit,
            delay_min_seconds=values.delay_min_seconds,
            delay_max_seconds=values.delay_max_seconds,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return QueueSettingsResponse(
        daily_limit=saved.daily_limit,
        delay_min_seconds=saved.delay_min_seconds,
        delay_max_seconds=saved.delay_max_seconds,
    )
