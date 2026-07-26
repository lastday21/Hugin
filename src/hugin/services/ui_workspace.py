from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    CareerDirectionModel,
    CoverLetterModel,
    DirectionVacancyModel,
    HhAccountModel,
    IncidentModel,
    InvitationModel,
    RecruiterMessageModel,
    ResumeModel,
    ScreeningAnswerModel,
    ScreeningFormModel,
    ScreeningQuestionModel,
    VacancyDiscoveryModel,
    VacancyModel,
)
from hugin.domain.content import CoverLetterState, IncidentState, ScreeningFormState
from hugin.domain.directions import VacancyState
from hugin.domain.tasks import TaskState
from hugin.domain.time import local_day_start_utc
from hugin.repositories.applications import ApplicationRepository
from hugin.services.queue import QueueService

ACTIVE_QUEUE_STATES = (
    TaskState.PENDING,
    TaskState.RUNNING,
    TaskState.RETRY_SCHEDULED,
)


@dataclass(frozen=True, slots=True)
class UiDirection:
    id: int
    name: str
    description: str | None
    is_active: bool
    queued: int
    rejected: int


@dataclass(frozen=True, slots=True)
class UiIncident:
    id: int
    code: str
    severity: str
    message: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UiDashboard:
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
    directions: tuple[UiDirection, ...]
    incidents: tuple[UiIncident, ...]


@dataclass(frozen=True, slots=True)
class UiQueueItem:
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


@dataclass(frozen=True, slots=True)
class UiRejectedVacancy:
    vacancy_id: str
    title: str
    company: str
    region: str
    source_url: str
    direction: str
    score: float | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UiQuestion:
    text: str
    answer: str | None
    source: str | None
    required: bool


@dataclass(frozen=True, slots=True)
class UiEvent:
    event_type: str
    created_at: datetime
    details: str


@dataclass(frozen=True, slots=True)
class UiVacancyCard:
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
    questions: tuple[UiQuestion, ...]
    events: tuple[UiEvent, ...]


class UiWorkspaceService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def dashboard(self, account_id: int) -> UiDashboard:
        account = self._account(account_id)
        queue = QueueService(self._session).status()
        applied_today = ApplicationRepository(self._session).count_applied_since(
            account_id,
            local_day_start_utc(),
        )
        directions = tuple(
            self._direction_summary(direction)
            for direction in self._session.scalars(
                select(CareerDirectionModel)
                .where(CareerDirectionModel.account_id == account_id)
                .order_by(CareerDirectionModel.name)
            )
        )
        pending_form_states = (
            ScreeningFormState.INPUT_REQUIRED,
            ScreeningFormState.REVIEW_REQUIRED,
        )
        pending_forms = (
            self._session.scalar(
                select(func.count())
                .select_from(ScreeningFormModel)
                .join(ApplicationModel, ApplicationModel.id == ScreeningFormModel.application_id)
                .where(
                    ApplicationModel.account_id == account_id,
                    ScreeningFormModel.state.in_(pending_form_states),
                )
            )
            or 0
        )
        ready_letters = (
            self._session.scalar(
                select(func.count())
                .select_from(CoverLetterModel)
                .join(ApplicationModel, ApplicationModel.id == CoverLetterModel.application_id)
                .where(
                    ApplicationModel.account_id == account_id,
                    CoverLetterModel.state == CoverLetterState.READY,
                )
            )
            or 0
        )
        rejected_vacancies = (
            self._session.scalar(
                select(func.count())
                .select_from(DirectionVacancyModel)
                .join(
                    CareerDirectionModel,
                    CareerDirectionModel.id == DirectionVacancyModel.direction_id,
                )
                .where(
                    CareerDirectionModel.account_id == account_id,
                    DirectionVacancyModel.state == VacancyState.FILTERED_OUT,
                )
            )
            or 0
        )
        new_messages = (
            self._session.scalar(
                select(func.count())
                .select_from(RecruiterMessageModel)
                .join(ApplicationModel, ApplicationModel.id == RecruiterMessageModel.application_id)
                .where(ApplicationModel.account_id == account_id)
            )
            or 0
        )
        invitations = (
            self._session.scalar(
                select(func.count())
                .select_from(InvitationModel)
                .join(ApplicationModel, ApplicationModel.id == InvitationModel.application_id)
                .where(ApplicationModel.account_id == account_id)
            )
            or 0
        )
        incident_models = self._session.scalars(
            select(IncidentModel)
            .where(IncidentModel.state == IncidentState.OPEN)
            .order_by(IncidentModel.created_at.desc())
            .limit(5)
        )
        incidents = tuple(
            UiIncident(
                incident.id,
                incident.code,
                incident.severity.value,
                incident.message,
                incident.created_at,
            )
            for incident in incident_models
        )
        return UiDashboard(
            account_label=account.label,
            system_state=queue.system.state.value,
            next_apply_at=queue.system.next_apply_at,
            daily_limit=queue.policy.daily_limit,
            delay_min_seconds=queue.policy.delay_min_seconds,
            delay_max_seconds=queue.policy.delay_max_seconds,
            applied_today=applied_today,
            remaining_today=max(queue.policy.daily_limit - applied_today, 0),
            task_counts={state.value: count for state, count in queue.task_counts.items()},
            pending_forms=pending_forms,
            ready_letters=ready_letters,
            rejected_vacancies=rejected_vacancies,
            new_messages=new_messages,
            invitations=invitations,
            directions=directions,
            incidents=incidents,
        )

    def queue(self, account_id: int, limit: int = 100) -> tuple[UiQueueItem, ...]:
        self._account(account_id)
        letter_state = (
            select(CoverLetterModel.state)
            .where(CoverLetterModel.application_id == ApplicationModel.id)
            .order_by(CoverLetterModel.id.desc())
            .limit(1)
            .correlate(ApplicationModel)
            .scalar_subquery()
        )
        form_state = (
            select(ScreeningFormModel.state)
            .where(ScreeningFormModel.application_id == ApplicationModel.id)
            .order_by(ScreeningFormModel.id.desc())
            .limit(1)
            .correlate(ApplicationModel)
            .scalar_subquery()
        )
        rows = self._session.execute(
            select(
                ApplicationTaskModel,
                ApplicationModel,
                VacancyModel,
                ResumeModel,
                CareerDirectionModel,
                letter_state,
                form_state,
            )
            .join(ApplicationModel, ApplicationModel.id == ApplicationTaskModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .join(ResumeModel, ResumeModel.id == ApplicationModel.resume_id)
            .outerjoin(
                CareerDirectionModel,
                CareerDirectionModel.id == ApplicationModel.direction_id,
            )
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationTaskModel.state.in_(ACTIVE_QUEUE_STATES),
            )
            .order_by(
                ApplicationTaskModel.state,
                ApplicationTaskModel.priority_score.desc(),
                ApplicationTaskModel.scheduled_at,
            )
            .limit(limit)
        )
        return tuple(
            UiQueueItem(
                task_id=task.id,
                vacancy_id=vacancy.hh_id,
                title=vacancy.title,
                company=vacancy.employer_name or "Компания не указана",
                region=vacancy.region or "Регион не указан",
                source_url=vacancy.source_url,
                resume_title=resume.title,
                direction=direction.name if direction is not None else "Без направления",
                state=task.state.value,
                priority=task.priority_score,
                scheduled_at=task.scheduled_at,
                last_error=task.last_error_code,
                letter_state=stored_letter_state.value if stored_letter_state is not None else None,
                form_state=stored_form_state.value if stored_form_state is not None else None,
            )
            for (
                task,
                _application,
                vacancy,
                resume,
                direction,
                stored_letter_state,
                stored_form_state,
            ) in rows
        )

    def rejected(self, account_id: int, limit: int = 100) -> tuple[UiRejectedVacancy, ...]:
        self._account(account_id)
        rows = self._session.execute(
            select(VacancyModel, DirectionVacancyModel, CareerDirectionModel)
            .join(
                DirectionVacancyModel,
                DirectionVacancyModel.vacancy_id == VacancyModel.id,
            )
            .join(
                CareerDirectionModel,
                CareerDirectionModel.id == DirectionVacancyModel.direction_id,
            )
            .where(
                CareerDirectionModel.account_id == account_id,
                DirectionVacancyModel.state == VacancyState.FILTERED_OUT,
            )
            .order_by(DirectionVacancyModel.updated_at.desc())
            .limit(limit)
        )
        return tuple(
            UiRejectedVacancy(
                vacancy_id=vacancy.hh_id,
                title=vacancy.title,
                company=vacancy.employer_name or "Компания не указана",
                region=vacancy.region or "Регион не указан",
                source_url=vacancy.source_url,
                direction=direction.name,
                score=tracking.rules_score,
                reasons=self._reasons(tracking.rules_details),
            )
            for vacancy, tracking, direction in rows
        )

    def vacancy(self, account_id: int, hh_id: str) -> UiVacancyCard:
        self._account(account_id)
        row = self._session.execute(
            select(VacancyModel, DirectionVacancyModel, CareerDirectionModel)
            .join(
                DirectionVacancyModel,
                DirectionVacancyModel.vacancy_id == VacancyModel.id,
            )
            .join(
                CareerDirectionModel,
                CareerDirectionModel.id == DirectionVacancyModel.direction_id,
            )
            .where(
                CareerDirectionModel.account_id == account_id,
                VacancyModel.hh_id == hh_id,
            )
            .order_by(DirectionVacancyModel.updated_at.desc())
            .limit(1)
        ).first()
        if row is None:
            raise LookupError("Вакансия не найдена")
        vacancy, tracking, direction = row
        cover_letter = self._session.scalar(
            select(CoverLetterModel.text)
            .join(ApplicationModel, ApplicationModel.id == CoverLetterModel.application_id)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.vacancy_id == vacancy.id,
                CoverLetterModel.text.is_not(None),
            )
            .order_by(CoverLetterModel.id.desc())
            .limit(1)
        )
        form = self._session.scalar(
            select(ScreeningFormModel)
            .join(ApplicationModel, ApplicationModel.id == ScreeningFormModel.application_id)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.vacancy_id == vacancy.id,
            )
            .order_by(ScreeningFormModel.id.desc())
            .limit(1)
        )
        questions = self._questions(form.id) if form is not None else ()
        discoveries = tuple(
            f"{item.query_text} — {item.region or 'Россия'}"
            for item in self._session.scalars(
                select(VacancyDiscoveryModel)
                .where(VacancyDiscoveryModel.vacancy_id == vacancy.id)
                .order_by(VacancyDiscoveryModel.discovered_at)
            )
        )
        events = self._events(account_id, vacancy.id)
        return UiVacancyCard(
            vacancy_id=vacancy.hh_id,
            title=vacancy.title,
            company=vacancy.employer_name or "Компания не указана",
            source_url=vacancy.source_url,
            region=vacancy.region or "Регион не указан",
            address=vacancy.address or "Адрес не указан",
            salary=self._salary(vacancy),
            employment=vacancy.employment or "Занятость не указана",
            work_format=vacancy.work_format or "Формат не указан",
            experience=vacancy.experience or "Опыт не указан",
            skills=tuple(vacancy.key_skills),
            description=vacancy.description or "Описание пока не загружено",
            direction=direction.name,
            state=tracking.state.value,
            score=tracking.rules_score,
            reasons=self._reasons(tracking.rules_details),
            discoveries=discoveries,
            cover_letter=cover_letter,
            form_state=form.state.value if form is not None else None,
            questions=questions,
            events=events,
        )

    def _direction_summary(self, direction: CareerDirectionModel) -> UiDirection:
        counts: dict[VacancyState, int] = {
            state: count
            for state, count in self._session.execute(
                select(DirectionVacancyModel.state, func.count())
                .where(DirectionVacancyModel.direction_id == direction.id)
                .group_by(DirectionVacancyModel.state)
            )
        }
        queued = (
            self._session.scalar(
                select(func.count())
                .select_from(ApplicationTaskModel)
                .join(
                    ApplicationModel,
                    ApplicationModel.id == ApplicationTaskModel.application_id,
                )
                .where(
                    ApplicationModel.direction_id == direction.id,
                    ApplicationTaskModel.state.in_(ACTIVE_QUEUE_STATES),
                )
            )
            or 0
        )
        return UiDirection(
            id=direction.id,
            name=direction.name,
            description=direction.description,
            is_active=direction.is_active,
            queued=queued,
            rejected=counts.get(VacancyState.FILTERED_OUT, 0),
        )

    def _questions(self, form_id: int) -> tuple[UiQuestion, ...]:
        rows = self._session.execute(
            select(ScreeningQuestionModel, ScreeningAnswerModel)
            .outerjoin(
                ScreeningAnswerModel,
                ScreeningAnswerModel.question_id == ScreeningQuestionModel.id,
            )
            .where(ScreeningQuestionModel.form_id == form_id)
            .order_by(ScreeningQuestionModel.position)
        )
        return tuple(
            UiQuestion(
                text=question.question_text,
                answer=answer.answer_text if answer is not None else None,
                source=(
                    answer.source.value
                    if answer is not None and answer.source is not None
                    else None
                ),
                required=question.is_required,
            )
            for question, answer in rows
        )

    def _events(self, account_id: int, vacancy_id: int) -> tuple[UiEvent, ...]:
        rows = self._session.execute(
            select(ApplicationEventModel)
            .join(ApplicationModel, ApplicationModel.id == ApplicationEventModel.application_id)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.vacancy_id == vacancy_id,
            )
            .order_by(ApplicationEventModel.created_at.desc())
            .limit(20)
        )
        return tuple(
            UiEvent(
                event_type=event.event_type.value,
                created_at=event.created_at,
                details=str(
                    event.payload.get("confirmation") or event.payload.get("hh_status") or ""
                ),
            )
            for event in rows.scalars()
        )

    def _account(self, account_id: int) -> HhAccountModel:
        account = self._session.get(HhAccountModel, account_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru не найден")
        return account

    @staticmethod
    def _reasons(details: dict[str, object]) -> tuple[str, ...]:
        reasons = details.get("reasons", ())
        if not isinstance(reasons, list):
            return ()
        return tuple(str(reason) for reason in reasons)

    @staticmethod
    def _salary(vacancy: VacancyModel) -> str:
        if vacancy.salary_from is None and vacancy.salary_to is None:
            return "Зарплата не указана"
        start = (
            f"от {UiWorkspaceService._money(vacancy.salary_from)}"
            if vacancy.salary_from is not None
            else ""
        )
        end = (
            f"до {UiWorkspaceService._money(vacancy.salary_to)}"
            if vacancy.salary_to is not None
            else ""
        )
        currency = {"RUR": "₽", "RUB": "₽"}.get(
            vacancy.salary_currency or "",
            vacancy.salary_currency or "",
        )
        return " ".join(value for value in (start, end, currency) if value)

    @staticmethod
    def _money(value: Decimal) -> str:
        return f"{float(value):,.0f}".replace(",", " ")
