# ruff: noqa: RUF001
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    CareerDirectionModel,
    CoverLetterModel,
    DirectionVacancyModel,
    ScreeningFormModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationEventType
from hugin.domain.content import (
    CoverLetterState,
    ScreeningFormState,
    cover_letter_instruction_version,
)
from hugin.domain.tasks import TaskState
from hugin.domain.time import as_utc
from hugin.domain.vacancies import VacancyAvailability
from hugin.services.ai_prompts import AiPromptSettingsService
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE, RULES_VERSION

type ProgressStageKey = Literal[
    "awaiting_evaluation",
    "rejected",
    "ready",
    "preparing",
    "prepared",
    "sent",
    "review",
    "unavailable",
]
STAGES: dict[ProgressStageKey, tuple[str, str]] = {
    "awaiting_evaluation": (
        "Ожидают оценки",
        "Описание прочитано; действующий разбор ещё не завершён.",
    ),
    "rejected": ("Отсеяны", "Не подходят активным направлениям, являются дублями или устарели."),
    "ready": ("Подошли, ждут подготовки", "Отбор пройден; задание отклика ещё не создано."),
    "preparing": (
        "Отклик готовится",
        "Задание создано; письмо или обязательная анкета ещё готовятся.",
    ),
    "prepared": (
        "Письмо подготовлено",
        "Сохранено письмо текущей инструкции. Окончательные проверки выполняются перед отправкой.",
    ),
    "sent": (
        "Отклик уже отправлен",
        "Есть подтверждение отправки, в том числе до начала этих суток.",
    ),
    "review": (
        "Требуется действие",
        "Оценка, анкета или задание требуют решения либо результат неизвестен.",
    ),
    "unavailable": ("Недоступны", "Вакансия закрыта, архивирована или удалена."),
}
ATTENTION_TASKS = {TaskState.UNKNOWN_RESULT, TaskState.INPUT_REQUIRED, TaskState.REVIEW_REQUIRED}
ACTIVE_TASKS = {TaskState.PENDING, TaskState.RUNNING, TaskState.RETRY_SCHEDULED}
ATTENTION_FORMS = {
    ScreeningFormState.INPUT_REQUIRED,
    ScreeningFormState.REVIEW_REQUIRED,
    ScreeningFormState.INVALIDATED,
}


@dataclass(frozen=True, slots=True)
class ProgressStage:
    key: ProgressStageKey
    name: str
    description: str
    count: int


@dataclass(frozen=True, slots=True)
class DailyProgress:
    observed_at: datetime
    since: datetime
    total: int
    stages: tuple[ProgressStage, ...]
    oldest_pending_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProgressVacancy:
    vacancy_id: str
    title: str
    company: str
    stage: ProgressStageKey
    description: str
    read_at: datetime


@dataclass(frozen=True, slots=True)
class ProgressPage:
    observed_at: datetime
    since: datetime
    total: int
    items: tuple[ProgressVacancy, ...]
    offset: int
    limit: int


class VacancyProgressService:
    def __init__(self, session: Session, account_id: int) -> None:
        self._session = session
        self._account_id = account_id

    def snapshot(self, since: datetime) -> DailyProgress:
        observed_at = datetime.now(UTC)
        items = self._items(as_utc(since), observed_at)
        counts = Counter(item.stage for item in items)
        return DailyProgress(
            observed_at=observed_at,
            since=as_utc(since),
            total=len(items),
            stages=tuple(
                ProgressStage(key, name, description, counts[key])
                for key, (name, description) in STAGES.items()
            ),
            oldest_pending_at=min(
                (item.read_at for item in items if item.stage == "awaiting_evaluation"),
                default=None,
            ),
        )

    def vacancies(
        self, since: datetime, stage: str, *, offset: int = 0, limit: int = 25
    ) -> ProgressPage:
        if stage not in STAGES or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Неверная стадия или границы списка вакансий")
        observed_at = datetime.now(UTC)
        items = tuple(
            item for item in self._items(as_utc(since), observed_at) if item.stage == stage
        )
        return ProgressPage(
            observed_at, as_utc(since), len(items), items[offset : offset + limit], offset, limit
        )

    def _items(self, since: datetime, observed_at: datetime) -> tuple[ProgressVacancy, ...]:
        rows = self._session.execute(
            select(DirectionVacancyModel, VacancyModel, CareerDirectionModel)
            .join(VacancyModel, VacancyModel.id == DirectionVacancyModel.vacancy_id)
            .join(
                CareerDirectionModel, CareerDirectionModel.id == DirectionVacancyModel.direction_id
            )
            .where(
                CareerDirectionModel.account_id == self._account_id,
                VacancyModel.details_fetched_at >= since,
            )
            .order_by(VacancyModel.details_fetched_at, VacancyModel.id, CareerDirectionModel.id)
        ).all()
        if not rows:
            return ()
        grouped: dict[
            int, list[tuple[DirectionVacancyModel, VacancyModel, CareerDirectionModel]]
        ] = defaultdict(list)
        for tracked, vacancy, direction in rows:
            grouped[vacancy.id].append((tracked, vacancy, direction))
        semantic = BackgroundProcessService(self._session, self._account_id).semantic_statuses(rows)
        applications = self._session.execute(
            select(ApplicationModel, ApplicationTaskModel)
            .outerjoin(
                ApplicationTaskModel, ApplicationTaskModel.application_id == ApplicationModel.id
            )
            .where(
                ApplicationModel.account_id == self._account_id,
                ApplicationModel.vacancy_id.in_(grouped),
            )
        ).all()
        app_ids = {app.id for app, _ in applications}
        sent = (
            set(
                self._session.scalars(
                    select(ApplicationModel.vacancy_id)
                    .join(ApplicationEventModel)
                    .where(
                        ApplicationModel.id.in_(app_ids),
                        ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                        ApplicationEventModel.payload["hh_status"].as_string() == "APPLIED",
                    )
                )
            )
            if app_ids
            else set()
        )
        instruction = cover_letter_instruction_version(
            AiPromptSettingsService(self._session).get().cover_letter
        )
        letters = (
            {
                letter.application_id: letter
                for letter in self._session.scalars(
                    select(CoverLetterModel)
                    .where(
                        CoverLetterModel.application_id.in_(app_ids),
                        CoverLetterModel.instruction_version == instruction,
                    )
                    .order_by(CoverLetterModel.id)
                )
            }
            if app_ids
            else {}
        )
        forms = (
            {
                form.application_id: form
                for form in self._session.scalars(
                    select(ScreeningFormModel)
                    .where(ScreeningFormModel.application_id.in_(app_ids))
                    .order_by(
                        ScreeningFormModel.state != ScreeningFormState.INVALIDATED,
                        ScreeningFormModel.updated_at,
                        ScreeningFormModel.id,
                    )
                )
            }
            if app_ids
            else {}
        )
        by_vacancy: dict[int, list[tuple[ApplicationModel, ApplicationTaskModel | None]]] = (
            defaultdict(list)
        )
        for app, task in applications:
            by_vacancy[app.vacancy_id].append((app, task))
        items = []
        for vacancy_id, links in grouped.items():
            vacancy = links[0][1]
            stage, description = self._classify(
                links,
                by_vacancy[vacancy_id],
                semantic,
                letters,
                forms,
                vacancy_id in sent,
                instruction,
                observed_at,
            )
            assert vacancy.details_fetched_at is not None
            items.append(
                ProgressVacancy(
                    vacancy.hh_id,
                    vacancy.title,
                    vacancy.employer_name or "Компания не указана",
                    stage,
                    description,
                    as_utc(vacancy.details_fetched_at),
                )
            )
        return tuple(items)

    @staticmethod
    def _classify(
        links: list[tuple[DirectionVacancyModel, VacancyModel, CareerDirectionModel]],
        applications: list[tuple[ApplicationModel, ApplicationTaskModel | None]],
        semantic: dict[tuple[int, int], str],
        letters: dict[int, CoverLetterModel],
        forms: dict[int, ScreeningFormModel],
        sent: bool,
        instruction: str,
        observed_at: datetime,
    ) -> tuple[ProgressStageKey, str]:
        vacancy = links[0][1]
        if sent:
            return "sent", STAGES["sent"][1]
        if any(task and task.state in ATTENTION_TASKS for _, task in applications):
            return (
                "review",
                "Задание требует решения; неизвестный результат не считается отправкой.",
            )
        if vacancy.availability != VacancyAvailability.ACTIVE:
            return "unavailable", STAGES["unavailable"][1]
        if vacancy.duplicate_of_id is not None:
            return "rejected", "Вакансия объединена с другой карточкой как дубль."
        if vacancy.published_at and as_utc(vacancy.published_at) < observed_at - MAX_VACANCY_AGE:
            return "rejected", "Публикация старше допустимого срока поиска."
        active = [(tracked, direction) for tracked, _, direction in links if direction.is_active]
        if not active:
            return "review", "Все направления этой вакансии выключены."
        allowed, states = set(), []
        for tracked, direction in active:
            category = tracked.rules_details.get("category")
            status = semantic.get((direction.id, vacancy.id), "DISABLED")
            if tracked.rules_version != RULES_VERSION or status == "PENDING":
                states.append("awaiting_evaluation")
            elif category in {"MATCH", "STRETCH"} and status in {"DISABLED", "ALLOW"}:
                allowed.add(direction.id)
            elif category in {"REJECTED", "ROUTED"} or status == "REJECT":
                states.append("rejected")
            else:
                states.append("review")
        if not allowed:
            if "awaiting_evaluation" in states:
                return "awaiting_evaluation", STAGES["awaiting_evaluation"][1]
            if "review" in states:
                return "review", "Сохранённый отбор требует решения."
            return "rejected", STAGES["rejected"][1]
        eligible = [(app, task) for app, task in applications if app.direction_id in allowed]
        if any(forms.get(app.id) and forms[app.id].state in ATTENTION_FORMS for app, _ in eligible):
            return "review", "Обязательная анкета требует заполнения или подтверждения."
        current = [(app, task) for app, task in eligible if task and task.state in ACTIVE_TASKS]
        for app, _ in current:
            letter, form = letters.get(app.id), forms.get(app.id)
            if (
                form is not None
                and form.state not in {ScreeningFormState.CONFIRMED, ScreeningFormState.SENT}
            ) or (vacancy.has_screening_form and form is None):
                continue
            if letter and letter.quality_passed is False:
                return "review", "Подготовленное письмо не прошло проверку качества."
            if (
                letter
                and letter.state == CoverLetterState.READY
                and letter.text
                and letter.instruction_version == instruction
                and letter.vacancy_id == vacancy.id
                and letter.resume_id == app.resume_id
                and letter.direction_id == app.direction_id
            ):
                return "prepared", STAGES["prepared"][1]
        if current:
            return "preparing", STAGES["preparing"][1]
        if applications:
            return "review", "Отклик начат, но нет действующего задания или подтверждения отправки."
        return "ready", STAGES["ready"][1]
