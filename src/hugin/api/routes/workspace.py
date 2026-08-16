from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.adapters.hh_vacancy_status import HhVacancyStatusProbe
from hugin.api.dependencies import read_session, require_session_key, write_session
from hugin.database.models import (
    ApplicationModel,
    ApplicationSettingsModel,
    ApplicationTaskModel,
    VacancyModel,
)
from hugin.domain.applications import (
    ApplicationReconciliationResult,
    ReconciliationStatus,
)
from hugin.domain.content import AnswerSource
from hugin.domain.directions import EmploymentForm, SearchRegion, WorkFormat
from hugin.services.application_reconciliation import ApplicationReconciliationService
from hugin.services.automation import AutomationSchedulerService
from hugin.services.autonomy import AutonomyPolicy, AutonomyPolicyService
from hugin.services.career_directions import (
    COMMON_REGIONS,
    RUSSIA_REGION,
    CareerDirectionService,
)
from hugin.services.queue import QueueService
from hugin.services.screening_forms import ScreeningDraft, ScreeningDraftService
from hugin.services.ui_workspace import UiWorkspaceService


class RegionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    area: str
    name: str


class DirectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    role_scope: str
    is_active: bool
    queued: int
    rejected: int
    queries: tuple[str, ...]
    regions: tuple[RegionResponse, ...]
    work_formats: tuple[WorkFormat, ...]
    employment_forms: tuple[EmploymentForm, ...]
    minimum_salary: int | None
    desired_salary: int | None
    remote_all_russia: bool
    schedule_minutes: int


class DirectionOptionsResponse(BaseModel):
    regions: tuple[RegionResponse, ...]


class DirectionSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool
    queries: tuple[str, ...] = Field(min_length=1, max_length=20)
    regions: tuple[RegionResponse, ...] = Field(min_length=1, max_length=30)
    work_formats: tuple[WorkFormat, ...] = Field(max_length=3)
    employment_forms: tuple[EmploymentForm, ...] = Field(max_length=4)
    minimum_salary: int | None = Field(default=None, ge=1, strict=True)
    desired_salary: int | None = Field(default=None, ge=1, strict=True)
    remote_all_russia: bool
    schedule_minutes: int = Field(ge=5, le=1440, strict=True)


class IncidentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    severity: str
    message: str
    created_at: datetime


class BackgroundStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    last_success_at: datetime | None
    next_search_at: datetime | None
    next_messages_at: datetime | None
    next_statuses_at: datetime | None
    error: str | None


class DashboardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_label: str
    system_state: str
    search_enabled: bool
    resource_saving_mode: bool
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
    background: BackgroundStatusResponse
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
    decision_reasons: tuple[str, ...]


class SentApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    application_id: int
    vacancy_id: str
    title: str
    company: str
    region: str
    source_url: str
    resume_title: str
    direction: str
    state: str
    applied_at: datetime


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


class FormAnswerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(min_length=1, max_length=255)
    answer: str = Field(min_length=1, max_length=4000)


class FormAnswersUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: tuple[FormAnswerUpdate, ...] = Field(min_length=1, max_length=100)


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


class ReconciliationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ReconciliationStatus


class ReconciliationResponse(BaseModel):
    task_state: str
    application_state: str
    blocking: bool


class QueueSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_limit: int = Field(ge=25, strict=True)
    delay_min_seconds: int = Field(ge=0, strict=True)
    delay_max_seconds: int = Field(ge=0, strict=True)


class QueueSettingsResponse(BaseModel):
    daily_limit: int
    delay_min_seconds: int
    delay_max_seconds: int


class ApprovedReplyTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    incoming_text: str = Field(min_length=1, max_length=2_000)
    response_text: str = Field(min_length=1, max_length=5_000)
    enabled: bool = True


class AutonomyPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_apply_stretch: bool
    auto_submit_simple_forms: bool
    auto_prepare_replies: bool
    auto_send_approved_replies: bool
    auto_reconcile_unknown: bool
    reuse_confirmed_profile_facts: bool
    mark_opened_invitations_seen: bool
    mutable_fact_validity_days: int = Field(ge=1, le=365, strict=True)
    reply_templates: tuple[ApprovedReplyTemplateUpdate, ...] = Field(max_length=50)


class AutonomyPolicyResponse(AutonomyPolicyUpdate):
    revision: int


class SessionResponse(BaseModel):
    key: str


class BackgroundPreferencesResponse(BaseModel):
    search_enabled: bool
    resource_saving_mode: bool


class ResourceSavingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(strict=True)


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


@router.post("/search/pause", response_model=BackgroundPreferencesResponse)
def pause_search(
    session: WriteSession,
    _guard: SessionGuard,
) -> BackgroundPreferencesResponse:
    try:
        settings = AutomationSchedulerService(session).pause_search()
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _background_preferences(settings)


@router.post("/search/resume", response_model=BackgroundPreferencesResponse)
def resume_search(
    session: WriteSession,
    _guard: SessionGuard,
) -> BackgroundPreferencesResponse:
    try:
        settings = AutomationSchedulerService(session).resume_search()
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _background_preferences(settings)


@router.put(
    "/background/resource-saving",
    response_model=BackgroundPreferencesResponse,
)
def update_resource_saving(
    values: ResourceSavingUpdate,
    session: WriteSession,
    _guard: SessionGuard,
) -> BackgroundPreferencesResponse:
    try:
        settings = AutomationSchedulerService(session).set_resource_saving_mode(values.enabled)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _background_preferences(settings)


@router.get("/directions/options", response_model=DirectionOptionsResponse)
def direction_options() -> DirectionOptionsResponse:
    unique = {region.area: region for region in COMMON_REGIONS.values()}
    unique[RUSSIA_REGION.area] = RUSSIA_REGION
    return DirectionOptionsResponse(
        regions=tuple(
            RegionResponse(area=region.area, name=region.name)
            for region in sorted(unique.values(), key=lambda value: value.name)
        )
    )


@router.put("/directions/{direction_id}", response_model=DirectionResponse)
def update_direction(
    direction_id: int,
    values: DirectionSettingsUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> DirectionResponse:
    service = CareerDirectionService(session)
    try:
        service.update(
            account_id=account_id,
            direction_id=direction_id,
            is_active=values.is_active,
            queries=values.queries,
            regions=tuple(
                SearchRegion(area=region.area.strip(), name=region.name.strip())
                for region in values.regions
            ),
            work_formats=values.work_formats,
            employment_forms=values.employment_forms,
            minimum_salary=values.minimum_salary,
            desired_salary=values.desired_salary,
            remote_all_russia=values.remote_all_russia,
            schedule_minutes=values.schedule_minutes,
        )
        direction = next(
            item
            for item in UiWorkspaceService(session).dashboard(account_id).directions
            if item.id == direction_id
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (StopIteration, ValueError) as error:
        message = str(error) or "Направление не сохранилось"
        raise HTTPException(status_code=422, detail=message) from error
    return DirectionResponse.model_validate(direction)


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
    return tuple(_form_response(draft) for draft in drafts)


@router.post("/forms/reconcile", response_model=tuple[FormDraftResponse, ...])
def reconcile_forms(
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> tuple[FormDraftResponse, ...]:
    service = ScreeningDraftService(session)
    checked_at = datetime.now(UTC)
    checks = service.pending_availability_checks(
        account_id,
        checked_before=checked_at - timedelta(minutes=15),
    )
    if checks:
        probe = HhVacancyStatusProbe()
        with ThreadPoolExecutor(max_workers=min(8, len(checks))) as pool:
            results = tuple(pool.map(lambda item: probe.check(item.source_url), checks))
        for check, availability in zip(checks, results, strict=True):
            if availability is not None:
                service.record_availability_check(
                    account_id,
                    check.form_id,
                    availability,
                    checked_at=checked_at,
                )
    service.reconcile_pending_answers(account_id)
    return tuple(_form_response(draft) for draft in service.list_pending(account_id))


@router.post("/forms/{form_id}/answers", response_model=FormDraftResponse)
def save_form_answers(
    form_id: int,
    values: FormAnswersUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> FormDraftResponse:
    answer_map = {item.field_key: item.answer for item in values.answers}
    if len(answer_map) != len(values.answers):
        raise HTTPException(status_code=422, detail="Ответы содержат повторяющиеся поля")
    try:
        draft = ScreeningDraftService(session).save_confirmed_answers(
            account_id,
            form_id,
            answer_map,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _form_response(draft)


def _form_response(draft: ScreeningDraft) -> FormDraftResponse:
    return FormDraftResponse(
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
        questions=tuple(FormQuestionResponse(**asdict(question)) for question in draft.questions),
    )


@router.get("/rejected", response_model=tuple[RejectedVacancyResponse, ...])
def rejected(
    session: ReadSession,
    account_id: int = Query(default=1, ge=1),
    limit: int = Query(default=1000, ge=1, le=2000),
) -> tuple[RejectedVacancyResponse, ...]:
    try:
        return tuple(
            RejectedVacancyResponse.model_validate(item)
            for item in UiWorkspaceService(session).rejected(account_id, limit)
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/sent", response_model=tuple[SentApplicationResponse, ...])
def sent(
    session: ReadSession,
    account_id: int = Query(default=1, ge=1),
    limit: int = Query(default=1000, ge=1, le=2000),
) -> tuple[SentApplicationResponse, ...]:
    try:
        return tuple(
            SentApplicationResponse.model_validate(item)
            for item in UiWorkspaceService(session).sent(account_id, limit)
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


@router.post(
    "/queue/{task_id}/reconcile",
    response_model=ReconciliationResponse,
)
def reconcile_application(
    task_id: int,
    values: ReconciliationUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> ReconciliationResponse:
    if values.status not in {ReconciliationStatus.APPLIED, ReconciliationStatus.NOT_FOUND}:
        raise HTTPException(
            status_code=422,
            detail="Вручную можно указать только наличие или отсутствие отклика в истории hh.ru",
        )
    row = session.execute(
        select(ApplicationTaskModel, ApplicationModel, VacancyModel)
        .join(ApplicationModel, ApplicationModel.id == ApplicationTaskModel.application_id)
        .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
        .where(
            ApplicationTaskModel.id == task_id,
            ApplicationModel.account_id == account_id,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Отклик не найден")
    _task, _application, vacancy = row
    confirmation = (
        "Пользователь подтвердил наличие отклика в истории hh.ru"
        if values.status is ReconciliationStatus.APPLIED
        else "Пользователь подтвердил отсутствие отклика в истории hh.ru"
    )
    try:
        outcome = ApplicationReconciliationService(session).reconcile(
            task_id,
            ApplicationReconciliationResult(
                status=values.status,
                final_url=vacancy.source_url,
                confirmation=confirmation,
                checked_at=datetime.now(UTC),
            ),
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return ReconciliationResponse(
        task_state=outcome.task.state.value,
        application_state=outcome.application.state.value,
        blocking=outcome.blocking,
    )


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


@router.get("/autonomy", response_model=AutonomyPolicyResponse)
def autonomy_policy(session: ReadSession) -> AutonomyPolicyResponse:
    try:
        return _autonomy_response(AutonomyPolicyService(session).get())
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put("/autonomy", response_model=AutonomyPolicyResponse)
def update_autonomy_policy(
    values: AutonomyPolicyUpdate,
    session: WriteSession,
    _guard: SessionGuard,
) -> AutonomyPolicyResponse:
    try:
        saved = AutonomyPolicyService(session).update(values.model_dump(mode="json"))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _autonomy_response(saved)


def _autonomy_response(policy: AutonomyPolicy) -> AutonomyPolicyResponse:
    return AutonomyPolicyResponse(
        revision=policy.revision,
        auto_apply_stretch=policy.auto_apply_stretch,
        auto_submit_simple_forms=policy.auto_submit_simple_forms,
        auto_prepare_replies=policy.auto_prepare_replies,
        auto_send_approved_replies=policy.auto_send_approved_replies,
        auto_reconcile_unknown=policy.auto_reconcile_unknown,
        reuse_confirmed_profile_facts=policy.reuse_confirmed_profile_facts,
        mark_opened_invitations_seen=policy.mark_opened_invitations_seen,
        mutable_fact_validity_days=policy.mutable_fact_validity_days,
        reply_templates=tuple(
            ApprovedReplyTemplateUpdate(
                key=template.key,
                incoming_text=template.incoming_text,
                response_text=template.response_text,
                enabled=template.enabled,
            )
            for template in policy.reply_templates
        ),
    )


def _background_preferences(
    settings: ApplicationSettingsModel,
) -> BackgroundPreferencesResponse:
    return BackgroundPreferencesResponse(
        search_enabled=settings.search_enabled,
        resource_saving_mode=settings.resource_saving_mode,
    )
