from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    CoverLetterModel,
    DirectionVacancyModel,
    ResumeModel,
    VacancyModel,
)
from hugin.domain.applications import (
    ApplicationEventType,
    ApplicationRecord,
    ApplicationState,
    EventPayload,
)
from hugin.domain.automation import AutomationJobKind
from hugin.domain.content import (
    CoverLetterState,
    ScreeningFormState,
    cover_letter_instruction_version,
)
from hugin.domain.directions import (
    AccountRecord,
    DirectionRecord,
    DirectionVacancyRecord,
    ResumeRecord,
    VacancyState,
)
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.tasks import ApplicationPolicyRecord, SystemState, TaskRecord, TaskState
from hugin.domain.time import as_utc
from hugin.domain.vacancies import VacancyAvailability, VacancyRecord
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.automation import AutomationJobRepository
from hugin.repositories.directions import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
)
from hugin.repositories.tasks import (
    FORM_PREFLIGHT_PASSED,
    FORM_PREFLIGHT_RUNNING,
    QueueTaskRepository,
    SystemStateRepository,
)
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.ai_prompts import AiPromptSettingsService
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.cover_letter import CoverLetterService
from hugin.services.queue import QueueService
from hugin.services.screening_forms import ScreeningDraft, ScreeningDraftService
from hugin.services.vacancy_analysis import RULES_VERSION, RuleCategory

SUPERVISED_MIN_INTERVAL = timedelta(seconds=60)


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
    cover_letter_id: int | None = None
    cover_letter_sha256: str | None = None


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
            vacancy = self._vacancies.get(tracked.vacancy_id)
            category = tracked.rules_details.get("category")
            if (
                tracked.rules_version != RULES_VERSION
                or tracked.state not in {VacancyState.ANALYZED, VacancyState.QUEUED}
                or category not in allowed
                or vacancy.duplicate_of_id is not None
            ):
                continue
            current = self._applications.get_by_key(account.id, tracked.vacancy_id, resume.id)
            if current is not None:
                task = self._tasks.get_by_application_id(current.id)
                if current.direction_id != direction.id:
                    if self._can_reassign_routed_application(
                        current,
                        task,
                        direction,
                        tracked.vacancy_id,
                    ):
                        self._applications.reassign_direction(current.id, direction.id)
                        if task is None:
                            self._tasks.enqueue(current.id, self._priority(tracked))
                        else:
                            self._tasks.requeue_after_rule_change(
                                task.id,
                                priority_score=self._priority(tracked),
                            )
                        self._directions.set_vacancy_state(
                            direction.id,
                            tracked.vacancy_id,
                            VacancyState.QUEUED,
                        )
                        created += 1
                        continue
                    existing += 1
                    continue
                if task is None and current.state is ApplicationState.APPLYING:
                    self._tasks.enqueue(current.id, self._priority(tracked))
                    self._directions.set_vacancy_state(
                        direction.id,
                        tracked.vacancy_id,
                        VacancyState.QUEUED,
                    )
                    created += 1
                else:
                    if (
                        task is not None
                        and task.state is TaskState.SKIPPED
                        and task.last_error_code == "VACANCY_RULES_CHANGED"
                    ):
                        self._tasks.requeue_after_rule_change(
                            task.id,
                            priority_score=self._priority(tracked),
                        )
                        self._directions.set_vacancy_state(
                            direction.id,
                            tracked.vacancy_id,
                            VacancyState.QUEUED,
                        )
                        created += 1
                    elif task is not None and task.state not in {
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
        self._tasks.skip_ineligible(
            direction.id,
            rules_version=RULES_VERSION,
            allowed_categories=frozenset(allowed),
        )
        return PreparationResult(account.id, direction.id, resume, created, existing)

    def _can_reassign_routed_application(
        self,
        application: ApplicationRecord,
        task: TaskRecord | None,
        target_direction: DirectionRecord,
        vacancy_id: int,
    ) -> bool:
        if application.state is not ApplicationState.APPLYING:
            return False
        if task is not None and (
            task.state is not TaskState.SKIPPED or task.last_error_code != "VACANCY_RULES_CHANGED"
        ):
            return False
        if application.direction_id is None:
            return False
        try:
            source = self._directions.get_tracked_vacancy(
                application.direction_id,
                vacancy_id,
            )
        except LookupError:
            return False
        return (
            source.rules_details.get("category") == RuleCategory.ROUTED.value
            and source.rules_details.get("target_scope") == target_direction.scope.value
        )

    @staticmethod
    def _priority(tracked: DirectionVacancyRecord) -> float:
        priority = tracked.rules_score or 0
        if tracked.rules_details.get("category") == RuleCategory.STRETCH.value:
            return max(priority - 20, 0)
        return priority

    def recover_interrupted(self) -> int:
        if self._system.supervised_lease_active():
            return 0
        recovered = self._tasks.recover_running()
        return len(recovered)

    def recover_expired_supervised(self, now: datetime | None = None) -> int:
        selected_at = now or datetime.now(UTC)
        if not self._system.clear_expired_supervised_lease(selected_at):
            return 0
        recovered = self._tasks.recover_running(
            recovery="supervised_lease_expired",
            now=selected_at,
        )
        return len(recovered)

    def policy(self, timezone_name: str | None = None) -> ApplicationPolicyRecord:
        return self._queue.policy(timezone_name)

    def applications_enabled(self) -> bool:
        return (
            self._system.get().state is SystemState.RUNNING
            and not self._system.supervised_lease_active()
        )

    def stretch_automation_enabled(self) -> bool:
        return AutonomyPolicyService(self._session).get().auto_apply_stretch

    def acquire_supervised_lease(
        self,
        token: str,
        *,
        ttl: timedelta = timedelta(minutes=15),
    ) -> datetime:
        self._system.lock()
        running_task = self._session.scalar(
            select(ApplicationTaskModel.id)
            .where(ApplicationTaskModel.state == TaskState.RUNNING)
            .limit(1)
        )
        if running_task is not None:
            raise RuntimeError(
                "Дождитесь завершения уже начатого фонового действия и повторите запуск"
            )
        return self._system.acquire_supervised_lease(token, ttl=ttl)

    def release_supervised_lease(self, token: str) -> None:
        self._system.release_supervised_lease(token)

    def supervised_lease_is_valid(self, token: str) -> bool:
        return self._system.supervised_lease_is_valid(token)

    def supervised_submission_is_allowed(
        self,
        token: str,
        task_id: int,
        *,
        letter_id: int,
        letter_sha256: str,
        resume_hh_id: str,
        resume_title: str,
        now: datetime | None = None,
    ) -> bool:
        selected_at = as_utc(now or datetime.now(UTC))
        system = self._system.lock()
        if not self._system.supervised_lease_is_valid(token, now=selected_at):
            return False
        instruction_version = cover_letter_instruction_version(
            AiPromptSettingsService(self._session).get().cover_letter
        )
        row = self._session.execute(
            select(CoverLetterModel, ResumeModel, ApplicationModel)
            .join(
                ApplicationModel,
                ApplicationModel.id == CoverLetterModel.application_id,
            )
            .join(
                ApplicationTaskModel,
                ApplicationTaskModel.application_id == ApplicationModel.id,
            )
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .join(ResumeModel, ResumeModel.id == ApplicationModel.resume_id)
            .join(
                DirectionVacancyModel,
                (DirectionVacancyModel.direction_id == ApplicationModel.direction_id)
                & (DirectionVacancyModel.vacancy_id == ApplicationModel.vacancy_id),
            )
            .where(
                ApplicationTaskModel.id == task_id,
                ApplicationTaskModel.state == TaskState.RUNNING,
                ApplicationModel.state == ApplicationState.APPLYING,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                VacancyModel.duplicate_of_id.is_(None),
                DirectionVacancyModel.state == VacancyState.QUEUED,
                DirectionVacancyModel.rules_version == RULES_VERSION,
                DirectionVacancyModel.rules_details["category"]
                .as_string()
                .in_((RuleCategory.MATCH.value, RuleCategory.STRETCH.value)),
                CoverLetterModel.id == letter_id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
                func.length(func.btrim(CoverLetterModel.text)) > 0,
                CoverLetterModel.instruction_version == instruction_version,
                CoverLetterModel.resume_id == ApplicationModel.resume_id,
                CoverLetterModel.vacancy_id == ApplicationModel.vacancy_id,
                ResumeModel.is_active.is_(True),
                ResumeModel.hh_id == resume_hh_id,
                ResumeModel.title == resume_title,
            )
            .with_for_update()
        ).first()
        if row is None:
            return False
        letter, resume, application = row
        previous_confirmed_at = self.last_confirmed_application_at(
            application.account_id,
            before=selected_at,
        )
        not_before = system.next_apply_at
        if previous_confirmed_at is not None:
            confirmed_not_before = as_utc(previous_confirmed_at) + SUPERVISED_MIN_INTERVAL
            not_before = (
                max(as_utc(not_before), confirmed_not_before)
                if not_before is not None
                else confirmed_not_before
            )
        if not_before is not None and as_utc(not_before) > selected_at:
            return False
        if letter.text is None or not letter.text.strip():
            return False
        if (
            hashlib.sha256(letter.text.encode("utf-8")).hexdigest()
            != letter_sha256.strip().casefold()
        ):
            return False
        if as_utc(resume.updated_at) > as_utc(letter.updated_at):
            return False
        try:
            CoverLetterService(self._session).validate_for_submission(
                application_id=application.id,
                letter_id=letter.id,
            )
        except (LookupError, RuntimeError, ValueError):
            return False
        return True

    def background_submission_is_allowed(
        self,
        task_id: int,
        *,
        letter_id: int,
        letter_sha256: str,
        resume_hh_id: str,
        resume_title: str,
        now: datetime | None = None,
    ) -> bool:
        selected_at = as_utc(now or datetime.now(UTC))
        system = self._system.lock()
        if system.state is not SystemState.RUNNING or self._system.supervised_lease_active(
            selected_at
        ):
            return False
        if system.next_apply_at is not None and as_utc(system.next_apply_at) > selected_at:
            return False
        allowed_categories = [RuleCategory.MATCH.value]
        if self.stretch_automation_enabled():
            allowed_categories.append(RuleCategory.STRETCH.value)
        instruction_version = cover_letter_instruction_version(
            AiPromptSettingsService(self._session).get().cover_letter
        )
        row = self._session.execute(
            select(CoverLetterModel, ResumeModel, ApplicationModel)
            .join(
                ApplicationModel,
                ApplicationModel.id == CoverLetterModel.application_id,
            )
            .join(
                ApplicationTaskModel,
                ApplicationTaskModel.application_id == ApplicationModel.id,
            )
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .join(ResumeModel, ResumeModel.id == ApplicationModel.resume_id)
            .join(
                DirectionVacancyModel,
                (DirectionVacancyModel.direction_id == ApplicationModel.direction_id)
                & (DirectionVacancyModel.vacancy_id == ApplicationModel.vacancy_id),
            )
            .where(
                ApplicationTaskModel.id == task_id,
                ApplicationTaskModel.state == TaskState.RUNNING,
                ApplicationModel.state == ApplicationState.APPLYING,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                DirectionVacancyModel.state == VacancyState.QUEUED,
                DirectionVacancyModel.rules_version == RULES_VERSION,
                DirectionVacancyModel.rules_details["category"]
                .as_string()
                .in_(allowed_categories),
                CoverLetterModel.id == letter_id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
                func.length(func.btrim(CoverLetterModel.text)) > 0,
                CoverLetterModel.instruction_version == instruction_version,
                CoverLetterModel.resume_id == ApplicationModel.resume_id,
                CoverLetterModel.vacancy_id == ApplicationModel.vacancy_id,
                ResumeModel.is_active.is_(True),
                ResumeModel.hh_id == resume_hh_id,
                ResumeModel.title == resume_title,
            )
            .with_for_update()
        ).first()
        if row is None:
            return False
        letter, resume, application = row
        if (
            letter.text is None
            or hashlib.sha256(letter.text.encode("utf-8")).hexdigest()
            != letter_sha256.strip().casefold()
            or as_utc(resume.updated_at) > as_utc(letter.updated_at)
        ):
            return False
        try:
            CoverLetterService(self._session).validate_for_submission(
                application_id=application.id,
                letter_id=letter.id,
            )
        except (LookupError, RuntimeError, ValueError):
            return False
        return True

    def claim_supervised(
        self,
        *,
        lease_token: str,
        letter_id: int,
        letter_sha256: str,
        task_id: int | None = None,
        vacancy_hh_id: str | None = None,
        account_id: int | None = None,
        day_start: datetime | None = None,
        session_limit: int = 20,
        now: datetime | None = None,
    ) -> ApplyJob:
        if (task_id is None) == (vacancy_hh_id is None):
            raise ValueError("Укажите либо номер задания, либо номер вакансии")
        if not 1 <= session_limit <= 20:
            raise ValueError("Предел управляемого сеанса должен быть от 1 до 20")
        selected_at = as_utc(now or datetime.now(UTC))
        if not self._system.supervised_lease_is_valid(lease_token, now=selected_at):
            raise RuntimeError(
                "Управляемый сеанс не активен или срок его аренды истёк"  # noqa: RUF001
            )
        instruction_version = cover_letter_instruction_version(
            AiPromptSettingsService(self._session).get().cover_letter
        )
        statement = (
            select(
                ApplicationTaskModel,
                ApplicationModel,
                VacancyModel,
                ResumeModel,
                DirectionVacancyModel,
                CoverLetterModel,
            )
            .join(ApplicationModel, ApplicationModel.id == ApplicationTaskModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .join(ResumeModel, ResumeModel.id == ApplicationModel.resume_id)
            .join(
                DirectionVacancyModel,
                (DirectionVacancyModel.direction_id == ApplicationModel.direction_id)
                & (DirectionVacancyModel.vacancy_id == ApplicationModel.vacancy_id),
            )
            .join(
                CoverLetterModel,
                CoverLetterModel.application_id == ApplicationModel.id,
            )
            .where(
                ApplicationTaskModel.state.in_((TaskState.PENDING, TaskState.RETRY_SCHEDULED)),
                ApplicationModel.state == ApplicationState.APPLYING,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                VacancyModel.duplicate_of_id.is_(None),
                DirectionVacancyModel.state == VacancyState.QUEUED,
                DirectionVacancyModel.rules_version == RULES_VERSION,
                CoverLetterModel.id == letter_id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
                func.length(func.btrim(CoverLetterModel.text)) > 0,
                CoverLetterModel.instruction_version == instruction_version,
                CoverLetterModel.resume_id == ApplicationModel.resume_id,
                CoverLetterModel.vacancy_id == ApplicationModel.vacancy_id,
                ResumeModel.is_active.is_(True),
            )
        )
        if task_id is not None:
            statement = statement.where(ApplicationTaskModel.id == task_id)
        else:
            statement = statement.where(VacancyModel.hh_id == vacancy_hh_id)
        row = self._session.execute(statement.limit(1)).first()
        if row is None:
            raise LookupError("Задание, актуальное письмо, резюме или решение правил не совпадают")
        task_model, application_model, vacancy_model, resume_model, _tracked, letter = row
        if account_id is not None and application_model.account_id != account_id:
            raise LookupError("Задание относится к другому аккаунту")
        policy = self.policy()
        effective_limit = min(session_limit, policy.daily_limit)
        if (
            day_start is not None
            and self.applied_since(
                application_model.account_id,
                day_start,
            )
            >= effective_limit
        ):
            raise RuntimeError(
                f"Предел управляемого сеанса на сегодня достигнут: {effective_limit}"
            )
        system = self._system.get()
        previous_confirmed_at = self.last_confirmed_application_at(
            application_model.account_id,
            before=selected_at,
        )
        not_before = system.next_apply_at
        if previous_confirmed_at is not None:
            confirmed_not_before = as_utc(previous_confirmed_at) + SUPERVISED_MIN_INTERVAL
            not_before = (
                max(as_utc(not_before), confirmed_not_before)
                if not_before is not None
                else confirmed_not_before
            )
        if not_before is not None and as_utc(not_before) > selected_at:
            raise RuntimeError(
                f"Следующая отправка разрешена не раньше {as_utc(not_before).isoformat()}"
            )
        if letter.text is None or not letter.text.strip():
            raise ValueError("Сопроводительное письмо не может быть пустым")
        actual_sha256 = hashlib.sha256(letter.text.encode("utf-8")).hexdigest()
        if actual_sha256 != letter_sha256.strip().casefold():
            raise ValueError("Текст письма изменился после утверждения")
        if resume_model.updated_at > letter.updated_at:
            raise ValueError("Резюме изменилось после подготовки письма")
        CoverLetterService(self._session).validate_for_submission(
            application_id=application_model.id,
            letter_id=letter.id,
        )
        claimed = self._tasks.claim_exact(task_model.id, selected_at)
        if claimed is None:
            raise RuntimeError(
                "Задание уже забрал другой процесс или время отправки ещё не наступило"
            )
        application = self._applications.get(application_model.id)
        if application.direction_id is None:
            raise RuntimeError("Направление отклика отсутствует")
        return ApplyJob(
            task=claimed,
            application=application,
            vacancy=self._vacancies.get(vacancy_model.id),
            resume=self._resumes.get(resume_model.id),
            direction_vacancy=self._directions.get_tracked_vacancy(
                application.direction_id,
                vacancy_model.id,
            ),
            cover_letter=letter.text,
            cover_letter_id=letter.id,
            cover_letter_sha256=actual_sha256,
        )

    def release_supervised_claim(
        self,
        lease_token: str,
        task_id: int,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> None:
        selected_at = as_utc(now or datetime.now(UTC))
        self._system.lock()
        if not self._system.supervised_lease_is_valid(lease_token, now=selected_at):
            raise RuntimeError("Управляемый сеанс уже завершён")
        task = self._tasks.get(task_id)
        if task.state is not TaskState.RUNNING:
            raise RuntimeError("Управляемое задание уже изменило состояние")
        self._tasks.transition(
            task_id,
            TaskState.RETRY_SCHEDULED,
            scheduled_at=selected_at,
            error_code=error_code,
        )

    def last_confirmed_application_at(
        self,
        account_id: int,
        *,
        before: datetime | None = None,
    ) -> datetime | None:
        statement = (
            select(func.max(ApplicationEventModel.created_at))
            .join(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                ApplicationEventModel.payload["hh_status"].as_string()
                == HhApplyStatus.APPLIED.value,
                func.coalesce(
                    ApplicationEventModel.payload["source"].as_string(),
                    "",
                )
                != "hh.ru",
            )
        )
        if before is not None:
            statement = statement.where(ApplicationEventModel.created_at < before)
        return self._session.scalar(statement)

    def claim_next(
        self,
        direction_id: int | None = None,
        *,
        account_id: int | None = None,
        require_cover_letter: bool = False,
        allow_paused_review: bool = False,
        include_stretch: bool = True,
    ) -> ApplyJob | None:
        instruction_version = cover_letter_instruction_version(
            AiPromptSettingsService(self._session).get().cover_letter
        )
        if allow_paused_review:
            system = self._system.lock()
            if system.state is not SystemState.PAUSED:
                raise RuntimeError("Проверка без отправки доступна только на паузе")
            if self._system.supervised_lease_active():
                raise RuntimeError("Проверка без отправки недоступна во время управляемого отклика")
            task = self._tasks.claim_next(
                account_id=account_id,
                direction_id=direction_id,
                require_ready_cover_letter=require_cover_letter,
                cover_letter_instruction_version=instruction_version,
                vacancy_rules_version=RULES_VERSION,
                vacancy_rule_categories=(
                    frozenset(
                        {
                            RuleCategory.MATCH.value,
                            RuleCategory.STRETCH.value,
                        }
                    )
                    if include_stretch
                    else frozenset({RuleCategory.MATCH.value})
                ),
            )
        else:
            task = self._queue.claim_next(
                account_id=account_id,
                direction_id=direction_id,
                require_ready_cover_letter=require_cover_letter,
                cover_letter_instruction_version=instruction_version,
                vacancy_rules_version=RULES_VERSION,
                vacancy_rule_categories=(
                    frozenset(
                        {
                            RuleCategory.MATCH.value,
                            RuleCategory.STRETCH.value,
                        }
                    )
                    if include_stretch
                    else frozenset({RuleCategory.MATCH.value})
                ),
            )
        if task is None:
            return None
        application = self._applications.get(task.application_id)
        if account_id is not None and application.account_id != account_id:
            raise RuntimeError("Задание отклика относится к другому аккаунту")
        if application.direction_id is None:
            raise RuntimeError("Направление отклика отсутствует")
        letter = self._session.scalar(
            select(CoverLetterModel)
            .where(
                CoverLetterModel.application_id == application.id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
                CoverLetterModel.instruction_version == instruction_version,
            )
            .order_by(CoverLetterModel.id.desc())
            .limit(1)
        )
        if require_cover_letter and (letter is None or not letter.text):
            raise RuntimeError("Готовое сопроводительное письмо отсутствует")
        if letter is not None:
            try:
                CoverLetterService(self._session).validate_for_submission(
                    application_id=application.id,
                    letter_id=letter.id,
                )
                if as_utc(self._resumes.get(application.resume_id).updated_at) > as_utc(
                    letter.updated_at
                ):
                    raise ValueError("Резюме изменилось после подготовки письма")
            except (LookupError, RuntimeError, ValueError):
                manual_review = CoverLetterService(self._session).handle_stale_ready_letter(
                    application_id=application.id,
                    letter_id=letter.id,
                )
                target = TaskState.REVIEW_REQUIRED if manual_review else TaskState.RETRY_SCHEDULED
                self._tasks.transition(
                    task.id,
                    target,
                    scheduled_at=datetime.now(UTC),
                    error_code="COVER_LETTER_STALE",
                )
                return None
        cover_letter = letter.text if letter is not None else None
        cover_letter_sha256 = (
            hashlib.sha256(cover_letter.encode("utf-8")).hexdigest()
            if cover_letter is not None
            else None
        )
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
            cover_letter_id=letter.id if letter is not None else None,
            cover_letter_sha256=cover_letter_sha256,
        )

    def claim_next_form_preflight(
        self,
        *,
        account_id: int,
        include_stretch: bool = True,
        now: datetime | None = None,
    ) -> ApplyJob | None:
        selected_at = as_utc(now or datetime.now(UTC))
        system = self._system.lock()
        if (
            system.state is not SystemState.RUNNING
            or self._system.supervised_lease_active(selected_at)
            or (system.next_apply_at is not None and as_utc(system.next_apply_at) > selected_at)
        ):
            return None
        instruction_version = cover_letter_instruction_version(
            AiPromptSettingsService(self._session).get().cover_letter
        )
        task = self._tasks.claim_next(
            selected_at,
            account_id=account_id,
            exclude_ready_cover_letter=True,
            cover_letter_instruction_version=instruction_version,
            vacancy_rules_version=RULES_VERSION,
            vacancy_rule_categories=(
                frozenset(
                    {
                        RuleCategory.MATCH.value,
                        RuleCategory.STRETCH.value,
                    }
                )
                if include_stretch
                else frozenset({RuleCategory.MATCH.value})
            ),
            running_error_code=FORM_PREFLIGHT_RUNNING,
        )
        if task is None:
            return None
        application = self._applications.get(task.application_id)
        if application.account_id != account_id:
            raise RuntimeError("Задание проверки формы относится к другому аккаунту")
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

    def claim_supervised_form_preflight(
        self,
        *,
        account_id: int,
        task_id: int,
        include_stretch: bool = False,
        now: datetime | None = None,
    ) -> ApplyJob:
        selected_at = as_utc(now or datetime.now(UTC))
        system = self._system.lock()
        if system.state is not SystemState.PAUSED:
            raise RuntimeError(
                "Перед управляемой проверкой формы поставьте отправку откликов на паузу"
            )
        if self._system.supervised_lease_active(selected_at):
            raise RuntimeError("Другой управляемый сеанс ещё не завершён")

        instruction_version = cover_letter_instruction_version(
            AiPromptSettingsService(self._session).get().cover_letter
        )
        allowed_categories = (
            (RuleCategory.MATCH.value, RuleCategory.STRETCH.value)
            if include_stretch
            else (RuleCategory.MATCH.value,)
        )
        current_letter = (
            select(CoverLetterModel.id)
            .where(
                CoverLetterModel.application_id == ApplicationModel.id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
                CoverLetterModel.instruction_version == instruction_version,
            )
            .exists()
        )
        row = self._session.execute(
            select(
                ApplicationTaskModel,
                ApplicationModel,
                VacancyModel,
                ResumeModel,
                DirectionVacancyModel,
            )
            .join(
                ApplicationModel,
                ApplicationModel.id == ApplicationTaskModel.application_id,
            )
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .join(ResumeModel, ResumeModel.id == ApplicationModel.resume_id)
            .join(
                DirectionVacancyModel,
                (DirectionVacancyModel.direction_id == ApplicationModel.direction_id)
                & (DirectionVacancyModel.vacancy_id == ApplicationModel.vacancy_id),
            )
            .where(
                ApplicationTaskModel.id == task_id,
                ApplicationTaskModel.state.in_((TaskState.PENDING, TaskState.RETRY_SCHEDULED)),
                ApplicationTaskModel.scheduled_at <= selected_at,
                ApplicationModel.account_id == account_id,
                ApplicationModel.state == ApplicationState.APPLYING,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                VacancyModel.duplicate_of_id.is_(None),
                ResumeModel.is_active.is_(True),
                DirectionVacancyModel.state == VacancyState.QUEUED,
                DirectionVacancyModel.rules_version == RULES_VERSION,
                DirectionVacancyModel.rules_details["category"].as_string().in_(allowed_categories),
                ~current_letter,
            )
            .limit(1)
        ).first()
        if row is None:
            raise LookupError(
                "Задание не готово к проверке формы, уже имеет актуальное письмо "
                "или больше не соответствует правилам"
            )

        task_model, application_model, vacancy_model, resume_model, _tracked = row
        claimed = self._tasks.claim_exact(
            task_model.id,
            selected_at,
            running_error_code=FORM_PREFLIGHT_RUNNING,
        )
        if claimed is None:
            raise RuntimeError(
                "Задание уже забрал другой процесс или время его обработки ещё не наступило"  # noqa: RUF001
            )
        application = self._applications.get(application_model.id)
        if application.direction_id is None:
            raise RuntimeError("Направление отклика отсутствует")
        return ApplyJob(
            task=claimed,
            application=application,
            vacancy=self._vacancies.get(vacancy_model.id),
            resume=self._resumes.get(resume_model.id),
            direction_vacancy=self._directions.get_tracked_vacancy(
                application.direction_id,
                vacancy_model.id,
            ),
        )

    def release_form_preflight(
        self,
        job: ApplyJob,
        *,
        now: datetime | None = None,
    ) -> None:
        task = self._tasks.get(job.task.id)
        if (
            task.state is not TaskState.RUNNING
            or task.last_error_code != FORM_PREFLIGHT_RUNNING
            or task.application_id != job.application.id
        ):
            raise RuntimeError("Предварительная проверка формы уже завершена")
        self._tasks.transition(
            task.id,
            TaskState.RETRY_SCHEDULED,
            scheduled_at=as_utc(now or datetime.now(UTC)),
            error_code=FORM_PREFLIGHT_PASSED,
        )

    def applied_since(self, account_id: int, since: datetime) -> int:
        return self._applications.count_applied_since(account_id, since)

    def resume_after_authentication(self) -> None:
        self._system.resume_after_authentication()

    def record_form_preflight(
        self,
        job: ApplyJob,
        result: HhApplyResult,
        *,
        now: datetime | None = None,
    ) -> bool:
        if result.status is not HhApplyStatus.QUESTIONS_REQUIRED:
            raise ValueError("Результат не содержит анкету работодателя")
        task = self._tasks.get(job.task.id)
        if (
            task.state is not TaskState.RUNNING
            or task.last_error_code != FORM_PREFLIGHT_RUNNING
            or task.application_id != job.application.id
        ):
            raise RuntimeError("Предварительная проверка формы уже завершена")
        selected_at = as_utc(now or datetime.now(UTC))
        draft = self._capture_screening_form(job, result)
        payload: EventPayload = {
            "hh_status": result.status.value,
            "confirmation": result.confirmation[:1000],
            "final_url": result.final_url[:1000],
            "question_count": len(draft.questions),
            "answered_count": len(draft.answers),
            "screening_form_state": draft.state.value,
        }
        if draft.state is ScreeningFormState.CONFIRMED:
            self._tasks.transition(
                job.task.id,
                TaskState.RETRY_SCHEDULED,
                scheduled_at=selected_at,
                error_code=FORM_PREFLIGHT_PASSED,
                event_payload=payload,
            )
            return True
        self._tasks.transition(
            job.task.id,
            (
                TaskState.INPUT_REQUIRED
                if draft.state is ScreeningFormState.INPUT_REQUIRED
                else TaskState.REVIEW_REQUIRED
            ),
            error_code=result.status.value,
            event_payload=payload,
        )
        return False

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
        if result.status is HhApplyStatus.APPLIED:
            payload["source"] = "hugin_send"
        elif result.status is HhApplyStatus.ALREADY_APPLIED:
            payload["source"] = "hh.ru"
        if result.retry_after_seconds is not None:
            payload["retry_after_seconds"] = result.retry_after_seconds
        if result.screening_form_version_hash is not None:
            payload["screening_form_version_hash"] = result.screening_form_version_hash
        if result.status in {HhApplyStatus.APPLIED, HhApplyStatus.ALREADY_APPLIED}:
            current_application = self._applications.get(job.application.id)
            current_task = self._tasks.get(job.task.id)
            if current_application.state is not ApplicationState.APPLYING:
                if current_task.state is TaskState.RUNNING:
                    current_task = self._tasks.transition(
                        current_task.id,
                        TaskState.COMPLETED,
                    )
                if current_task.state is not TaskState.COMPLETED:
                    raise RuntimeError(
                        "Подтверждённый на hh.ru отклик имеет незавершённое состояние задания"
                    )
                if result.status is HhApplyStatus.APPLIED:
                    recorded = self._session.scalar(
                        select(ApplicationEventModel.id)
                        .where(
                            ApplicationEventModel.application_id == current_application.id,
                            ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                            ApplicationEventModel.payload["hh_status"].as_string()
                            == HhApplyStatus.APPLIED.value,
                            func.coalesce(
                                ApplicationEventModel.payload["source"].as_string(),
                                "",
                            )
                            != "hh.ru",
                        )
                        .limit(1)
                    )
                    if recorded is None:
                        self._applications.append_event(
                            current_application.id,
                            ApplicationEventType.APPLIED,
                            payload,
                        )
                    self._mark_cover_letter_sent(
                        job.application.id,
                        job.cover_letter_id,
                        selected_at,
                    )
                ScreeningDraftService(self._session).mark_sent(
                    job.application.id,
                    version_hash=result.screening_form_version_hash,
                    sent_at=selected_at,
                )
                sent = result.status is HhApplyStatus.APPLIED
                next_apply_at = (
                    selected_at + apply_delay if sent and apply_delay is not None else None
                )
                if next_apply_at is not None:
                    self._system.set_next_apply_at(next_apply_at)
                return RecordedApplyResult(
                    blocking=False,
                    sent=sent,
                    next_apply_at=next_apply_at,
                )
            self._applications.transition_state(
                job.application.id,
                ApplicationState.APPLIED,
                payload,
            )
            if result.status is HhApplyStatus.APPLIED:
                self._mark_cover_letter_sent(
                    job.application.id,
                    job.cover_letter_id,
                    selected_at,
                )
            ScreeningDraftService(self._session).mark_sent(
                job.application.id,
                version_hash=result.screening_form_version_hash,
                sent_at=selected_at,
            )
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
            draft = self._capture_screening_form(job, result)
            payload["question_count"] = len(draft.questions)
            payload["answered_count"] = len(draft.answers)
            payload["screening_form_state"] = draft.state.value
            self._tasks.transition(
                job.task.id,
                (
                    TaskState.RETRY_SCHEDULED
                    if draft.state is ScreeningFormState.CONFIRMED
                    else TaskState.INPUT_REQUIRED
                    if draft.state is ScreeningFormState.INPUT_REQUIRED
                    else TaskState.REVIEW_REQUIRED
                ),
                scheduled_at=selected_at,
                error_code=result.status.value,
                event_payload=payload,
            )
            return RecordedApplyResult(blocking=False, sent=False)

        if result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED:
            if result.screening_form is not None:
                draft = ScreeningDraftService(self._session).capture(
                    job.application.id,
                    result.screening_form,
                    force_review=True,
                )
                payload["question_count"] = len(draft.questions)
                payload["answered_count"] = len(draft.answers)
                payload["screening_form_state"] = draft.state.value
                self._tasks.transition(
                    job.task.id,
                    TaskState.REVIEW_REQUIRED,
                    error_code=result.status.value,
                    event_payload=payload,
                )
                return RecordedApplyResult(blocking=False, sent=False)
            self._tasks.transition(
                job.task.id,
                TaskState.REVIEW_REQUIRED,
                error_code=result.status.value,
                event_payload=payload,
            )
            return RecordedApplyResult(blocking=False, sent=False)

        if result.status is HhApplyStatus.VACANCY_CLOSED:
            self._vacancies.mark_unavailable(
                job.vacancy.id,
                VacancyAvailability.CLOSED,
            )
            current_application = self._applications.get(job.application.id)
            current_task = self._tasks.get(job.task.id)
            if current_application.state is ApplicationState.APPLYING:
                self._applications.transition_state(
                    job.application.id,
                    ApplicationState.CLOSED,
                    payload,
                )
            elif current_application.state is not ApplicationState.CLOSED:
                raise RuntimeError("Закрытая вакансия имеет несовместимое состояние отклика")
            if current_task.state is TaskState.RUNNING:
                self._tasks.transition(
                    job.task.id,
                    TaskState.SKIPPED,
                    error_code=result.status.value,
                )
            elif current_task.state is not TaskState.SKIPPED:
                raise RuntimeError("Закрытая вакансия имеет несовместимое состояние задания")
            return RecordedApplyResult(blocking=False, sent=False)

        if result.status is HhApplyStatus.UNKNOWN_RESULT:
            self._tasks.transition(
                job.task.id,
                TaskState.UNKNOWN_RESULT,
                error_code=result.status.value,
                event_payload=payload,
            )
            if AutonomyPolicyService(self._session).get().auto_reconcile_unknown:
                AutomationJobRepository(self._session).schedule_soon(
                    kind=AutomationJobKind.STATUSES,
                    account_id=job.application.account_id,
                    run_at=selected_at + timedelta(seconds=30),
                )
            return RecordedApplyResult(blocking=False, sent=False)

        system_states = {
            HhApplyStatus.AUTH_REQUIRED: SystemState.AUTH_REQUIRED,
            HhApplyStatus.INVALID_CREDENTIALS: SystemState.AUTH_REQUIRED,
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
        if (
            result.status is HhApplyStatus.RETRYABLE_ERROR
            and result.retry_after_seconds is not None
        ):
            self._system.set_next_apply_at(retry_at)
        target_state = system_states.get(result.status)
        if target_state is not None:
            self._transition_system(target_state)
        return RecordedApplyResult(
            blocking=target_state is not None,
            sent=False,
            next_apply_at=(
                retry_at
                if result.status is HhApplyStatus.RETRYABLE_ERROR
                and result.retry_after_seconds is not None
                else None
            ),
        )

    def _capture_screening_form(
        self,
        job: ApplyJob,
        result: HhApplyResult,
    ) -> ScreeningDraft:
        draft_service = ScreeningDraftService(self._session)
        return (
            draft_service.capture(job.application.id, result.screening_form)
            if result.screening_form is not None
            else draft_service.capture_questions(job.application.id, result.questions)
        )

    def release_after_preview(
        self,
        job: ApplyJob,
        *,
        now: datetime | None = None,
    ) -> RecordedApplyResult:
        self._tasks.transition(
            job.task.id,
            TaskState.RETRY_SCHEDULED,
            scheduled_at=now or datetime.now(UTC),
            error_code="MANUAL_PREVIEW",
        )
        return RecordedApplyResult(blocking=False, sent=False)

    def _mark_cover_letter_sent(
        self,
        application_id: int,
        cover_letter_id: int | None,
        sent_at: datetime,
    ) -> None:
        if cover_letter_id is None:
            return
        letter = self._session.scalar(
            select(CoverLetterModel)
            .where(
                CoverLetterModel.id == cover_letter_id,
                CoverLetterModel.application_id == application_id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
            )
            .limit(1)
        )
        if letter is not None:
            letter.state = CoverLetterState.SENT
            letter.sent_at = sent_at
            self._session.flush()

    def _transition_system(self, target: SystemState) -> None:
        current = self._system.get().state
        if current is target:
            return
        if current is SystemState.RUNNING or (
            current is SystemState.PAUSED
            and target
            in {
                SystemState.AUTH_REQUIRED,
                SystemState.CAPTCHA_REQUIRED,
                SystemState.ACCOUNT_WARNING,
            }
        ):
            self._system.transition(target)
