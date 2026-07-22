from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import CoverLetterModel
from hugin.domain.applications import (
    ApplicationRecord,
    ApplicationState,
    EventPayload,
)
from hugin.domain.content import CoverLetterState
from hugin.domain.directions import (
    AccountRecord,
    DirectionVacancyRecord,
    ResumeRecord,
    VacancyState,
)
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.tasks import ApplicationPolicyRecord, SystemState, TaskRecord, TaskState
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
from hugin.services.screening_forms import ScreeningDraftService
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
    cover_letter: str | None = None


@dataclass(frozen=True, slots=True)
class RecordedApplyResult:
    blocking: bool
    sent: bool
    next_apply_at: datetime | None = None


class ApplicationAutomationService:
    def __init__(self, session: Session) -> None:
        self._session = session
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
        return self._prepare(account, direction_name, include_stretch)

    def prepare_for_account_id(
        self,
        *,
        account_id: int,
        direction_name: str,
        include_stretch: bool,
    ) -> PreparationResult:
        return self._prepare(self._accounts.get(account_id), direction_name, include_stretch)

    def _prepare(
        self,
        account: AccountRecord,
        direction_name: str,
        include_stretch: bool,
    ) -> PreparationResult:
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
            if (
                tracked.state not in {VacancyState.ANALYZED, VacancyState.QUEUED}
                or category not in allowed
            ):
                continue
            current = self._applications.get_by_key(account.id, tracked.vacancy_id, resume.id)
            if current is not None:
                task = self._tasks.get_by_application_id(current.id)
                if task is None and current.state is ApplicationState.APPLYING:
                    self._tasks.enqueue(current.id, self._priority(tracked))
                    self._directions.set_vacancy_state(
                        direction.id,
                        tracked.vacancy_id,
                        VacancyState.QUEUED,
                    )
                    created += 1
                else:
                    if task is not None and task.state not in {
                        TaskState.COMPLETED,
                        TaskState.SKIPPED,
                    }:
                        self._directions.set_vacancy_state(
                            direction.id,
                            tracked.vacancy_id,
                            VacancyState.QUEUED,
                        )
                    existing += 1
                continue
            application = self._applications.create_apply_intent(
                account.id,
                tracked.vacancy_id,
                resume.id,
                direction.id,
            )
            self._tasks.enqueue(application.id, self._priority(tracked))
            self._directions.set_vacancy_state(
                direction.id,
                tracked.vacancy_id,
                VacancyState.QUEUED,
            )
            created += 1
        return PreparationResult(account.id, direction.id, resume, created, existing)

    @staticmethod
    def _priority(tracked: DirectionVacancyRecord) -> float:
        priority = tracked.rules_score or 0
        if tracked.rules_details.get("category") == RuleCategory.STRETCH.value:
            return max(priority - 20, 0)
        return priority

    def recover_interrupted(self) -> int:
        return len(self._tasks.recover_running())

    def policy(self, timezone_name: str) -> ApplicationPolicyRecord:
        return self._queue.policy(timezone_name)

    def claim_next(
        self,
        direction_id: int,
        *,
        require_cover_letter: bool = False,
    ) -> ApplyJob | None:
        task = self._queue.claim_next(
            direction_id=direction_id,
            require_ready_cover_letter=require_cover_letter,
        )
        if task is None:
            return None
        application = self._applications.get(task.application_id)
        if application.direction_id is None:
            raise RuntimeError("Направление отклика отсутствует")
        cover_letter = self._session.scalar(
            select(CoverLetterModel.text)
            .where(
                CoverLetterModel.application_id == application.id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
            )
            .order_by(CoverLetterModel.id.desc())
            .limit(1)
        )
        if require_cover_letter and not cover_letter:
            raise RuntimeError("Готовое сопроводительное письмо отсутствует")
        return ApplyJob(
            task=task,
            application=application,
            vacancy=self._vacancies.get(application.vacancy_id),
            resume=self._resumes.get(application.resume_id),
            direction_vacancy=self._directions.get_tracked_vacancy(
                application.direction_id,
                application.vacancy_id,
            ),
            cover_letter=cover_letter,
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
        apply_delay: timedelta | None = None,
        now: datetime | None = None,
    ) -> RecordedApplyResult:
        selected_at = now or datetime.now(UTC)
        payload: EventPayload = {
            "hh_status": result.status.value,
            "confirmation": result.confirmation[:1000],
            "final_url": result.final_url[:1000],
        }
        if result.retry_after_seconds is not None:
            payload["retry_after_seconds"] = result.retry_after_seconds
        if result.status in {HhApplyStatus.APPLIED, HhApplyStatus.ALREADY_APPLIED}:
            self._applications.transition_state(
                job.application.id,
                ApplicationState.APPLIED,
                payload,
            )
            if result.status is HhApplyStatus.APPLIED:
                self._mark_cover_letter_sent(job.application.id, selected_at)
            self._tasks.transition(job.task.id, TaskState.COMPLETED)
            sent = result.status is HhApplyStatus.APPLIED
            next_apply_at = selected_at + apply_delay if sent and apply_delay is not None else None
            if next_apply_at is not None:
                self._system.set_next_apply_at(next_apply_at)
            return RecordedApplyResult(
                blocking=False,
                sent=sent,
                next_apply_at=next_apply_at,
            )

        if result.status is HhApplyStatus.QUESTIONS_REQUIRED:
            draft_service = ScreeningDraftService(self._session)
            draft = (
                draft_service.capture(job.application.id, result.screening_form)
                if result.screening_form is not None
                else draft_service.capture_questions(job.application.id, result.questions)
            )
            payload["question_count"] = len(draft.questions)
            payload["answered_count"] = len(draft.answers)
            payload["screening_form_state"] = draft.state.value
            self._tasks.transition(
                job.task.id,
                TaskState.INPUT_REQUIRED,
                error_code=result.status.value,
                event_payload=payload,
            )
            return RecordedApplyResult(blocking=False, sent=False)

        if result.status is HhApplyStatus.VACANCY_CLOSED:
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
            return RecordedApplyResult(blocking=False, sent=False)

        system_states = {
            HhApplyStatus.AUTH_REQUIRED: SystemState.AUTH_REQUIRED,
            HhApplyStatus.CAPTCHA_REQUIRED: SystemState.CAPTCHA_REQUIRED,
            HhApplyStatus.ACCOUNT_WARNING: SystemState.ACCOUNT_WARNING,
            HhApplyStatus.RESUME_MISMATCH: SystemState.PAUSED,
        }
        effective_retry_delay = (
            timedelta(seconds=result.retry_after_seconds)
            if result.status is HhApplyStatus.RETRYABLE_ERROR
            and result.retry_after_seconds is not None
            else retry_delay
        )
        retry_at = selected_at + effective_retry_delay
        self._tasks.transition(
            job.task.id,
            TaskState.RETRY_SCHEDULED,
            scheduled_at=retry_at,
            error_code=result.status.value,
        )
        if result.status is HhApplyStatus.RETRYABLE_ERROR:
            self._system.set_next_apply_at(retry_at)
        target_state = system_states.get(result.status)
        if target_state is not None:
            self._transition_system(target_state)
        return RecordedApplyResult(
            blocking=target_state is not None,
            sent=False,
            next_apply_at=(retry_at if result.status is HhApplyStatus.RETRYABLE_ERROR else None),
        )

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
        self._mark_cover_letter_sent(application.id, datetime.now(UTC))
        self._tasks.transition(task.id, TaskState.COMPLETED)
        return RecordedApplyResult(blocking=False, sent=True)

    def _mark_cover_letter_sent(self, application_id: int, sent_at: datetime) -> None:
        letter = self._session.scalar(
            select(CoverLetterModel)
            .where(
                CoverLetterModel.application_id == application_id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
            )
            .order_by(CoverLetterModel.id.desc())
            .limit(1)
        )
        if letter is not None:
            letter.state = CoverLetterState.SENT
            letter.sent_at = sent_at
            self._session.flush()

    def _transition_system(self, target: SystemState) -> None:
        if self._system.get().state is SystemState.RUNNING:
            self._system.transition(target)
