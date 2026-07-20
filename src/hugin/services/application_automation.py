from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from hugin.domain.applications import (
    ApplicationRecord,
    ApplicationState,
    EventPayload,
)
from hugin.domain.directions import DirectionVacancyRecord, ResumeRecord, VacancyState
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.tasks import SystemState, TaskRecord, TaskState
from hugin.domain.vacancies import VacancyRecord
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.directions import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
)
from hugin.repositories.tasks import QueueTaskRepository, SystemStateRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.queue import QueueService
from hugin.services.vacancy_analysis import RuleCategory


@dataclass(frozen=True, slots=True)
class PreparationResult:
    account_id: int
    direction_id: int
    resume: ResumeRecord
    created: int
    existing: int


@dataclass(frozen=True, slots=True)
class ApplyJob:
    task: TaskRecord
    application: ApplicationRecord
    vacancy: VacancyRecord
    resume: ResumeRecord
    direction_vacancy: DirectionVacancyRecord


@dataclass(frozen=True, slots=True)
class RecordedApplyResult:
    blocking: bool
    sent: bool


class ApplicationAutomationService:
    def __init__(self, session: Session) -> None:
        self._accounts = AccountRepository(session)
        self._directions = DirectionRepository(session)
        self._resumes = ResumeRepository(session)
        self._vacancies = VacancyRepository(session)
        self._applications = ApplicationRepository(session)
        self._tasks = QueueTaskRepository(session)
        self._system = SystemStateRepository(session)
        self._queue = QueueService(session)

    def prepare(
        self,
        *,
        account_external_id: str,
        direction_name: str,
        include_stretch: bool,
    ) -> PreparationResult:
        account = self._accounts.get_by_external_id(account_external_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru не найден в базе")
        direction = self._directions.get_by_account_and_name(account.id, direction_name)
        if direction is None:
            raise LookupError(f"Направление «{direction_name}» не найдено")
        resume = next(
            (item for item in self._directions.list_resumes(direction.id) if item.is_active),
            None,
        )
        if resume is None:
            raise LookupError("Активное резюме направления не найдено")

        allowed = {RuleCategory.MATCH.value}
        if include_stretch:
            allowed.add(RuleCategory.STRETCH.value)
        created = 0
        existing = 0
        for tracked in self._directions.list_tracked_vacancies(direction.id):
            category = tracked.rules_details.get("category")
            if tracked.state is not VacancyState.FILTERED or category not in allowed:
                continue
            current = self._applications.get_by_key(account.id, tracked.vacancy_id, resume.id)
            if current is not None:
                existing += 1
                continue
            application = self._applications.create_apply_intent(
                account.id,
                tracked.vacancy_id,
                resume.id,
                direction.id,
            )
            priority = tracked.rules_score or 0
            if category == RuleCategory.STRETCH.value:
                priority = max(priority - 20, 0)
            self._tasks.enqueue(application.id, priority)
            self._directions.set_vacancy_state(
                direction.id,
                tracked.vacancy_id,
                VacancyState.QUEUED,
            )
            created += 1
        return PreparationResult(account.id, direction.id, resume, created, existing)

    def claim_next(self, direction_id: int) -> ApplyJob | None:
        task = self._queue.claim_next(direction_id=direction_id)
        if task is None:
            return None
        application = self._applications.get(task.application_id)
        if application.direction_id is None:
            raise RuntimeError("Направление отклика отсутствует")
        return ApplyJob(
            task=task,
            application=application,
            vacancy=self._vacancies.get(application.vacancy_id),
            resume=self._resumes.get(application.resume_id),
            direction_vacancy=self._directions.get_tracked_vacancy(
                application.direction_id,
                application.vacancy_id,
            ),
        )

    def applied_since(self, account_id: int, since: datetime) -> int:
        return self._applications.count_applied_since(account_id, since)

    def resume_after_authentication(self) -> None:
        current = self._system.get().state
        if current in {SystemState.AUTH_REQUIRED, SystemState.CAPTCHA_REQUIRED}:
            self._system.transition(SystemState.RUNNING)

    def record_result(
        self,
        job: ApplyJob,
        result: HhApplyResult,
        *,
        retry_delay: timedelta = timedelta(minutes=15),
    ) -> RecordedApplyResult:
        payload: EventPayload = {
            "hh_status": result.status.value,
            "confirmation": result.confirmation[:1000],
            "final_url": result.final_url[:1000],
        }
        if result.status in {HhApplyStatus.APPLIED, HhApplyStatus.ALREADY_APPLIED}:
            self._applications.transition_state(
                job.application.id,
                ApplicationState.APPLIED,
                payload,
            )
            self._tasks.transition(job.task.id, TaskState.COMPLETED)
            return RecordedApplyResult(blocking=False, sent=result.status is HhApplyStatus.APPLIED)

        if result.status in {HhApplyStatus.QUESTIONS_REQUIRED, HhApplyStatus.VACANCY_CLOSED}:
            payload["question_count"] = len(result.questions)
            self._applications.transition_state(
                job.application.id,
                ApplicationState.CLOSED,
                payload,
            )
            self._tasks.transition(
                job.task.id,
                TaskState.SKIPPED,
                error_code=result.status.value,
            )
            return RecordedApplyResult(blocking=False, sent=False)

        if result.status is HhApplyStatus.UNKNOWN_RESULT:
            self._tasks.transition(
                job.task.id,
                TaskState.UNKNOWN_RESULT,
                error_code=result.status.value,
                event_payload=payload,
            )
            self._transition_system(SystemState.PAUSED)
            return RecordedApplyResult(blocking=True, sent=False)

        system_states = {
            HhApplyStatus.AUTH_REQUIRED: SystemState.AUTH_REQUIRED,
            HhApplyStatus.CAPTCHA_REQUIRED: SystemState.CAPTCHA_REQUIRED,
            HhApplyStatus.ACCOUNT_WARNING: SystemState.ACCOUNT_WARNING,
            HhApplyStatus.RESUME_MISMATCH: SystemState.PAUSED,
        }
        self._tasks.transition(
            job.task.id,
            TaskState.RETRY_SCHEDULED,
            scheduled_at=datetime.now(UTC) + retry_delay,
            error_code=result.status.value,
        )
        target_state = system_states.get(result.status)
        if target_state is not None:
            self._transition_system(target_state)
        return RecordedApplyResult(blocking=target_state is not None, sent=False)

    def confirm_unknown_as_applied(
        self,
        task_id: int,
        *,
        final_url: str,
        confirmation: str,
    ) -> RecordedApplyResult:
        task = self._tasks.get(task_id)
        if task.state is not TaskState.UNKNOWN_RESULT:
            raise ValueError("Task state must be UNKNOWN_RESULT")
        application = self._applications.get(task.application_id)
        self._applications.transition_state(
            application.id,
            ApplicationState.APPLIED,
            {
                "hh_status": HhApplyStatus.APPLIED.value,
                "confirmation": confirmation[:1000],
                "final_url": final_url[:1000],
            },
        )
        self._tasks.transition(task.id, TaskState.COMPLETED)
        if self._system.get().state is SystemState.PAUSED:
            self._system.transition(SystemState.RUNNING)
        return RecordedApplyResult(blocking=False, sent=True)

    def _transition_system(self, target: SystemState) -> None:
        if self._system.get().state is SystemState.RUNNING:
            self._system.transition(target)
