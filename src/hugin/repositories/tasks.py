from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationSettingsModel,
    ApplicationTaskModel,
    CareerDirectionModel,
    CoverLetterModel,
    DirectionVacancyModel,
    SystemStateModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationEventType, ApplicationState, EventPayload
from hugin.domain.content import CURRENT_COVER_LETTER_INSTRUCTION, CoverLetterState
from hugin.domain.directions import DirectionScope, VacancyState
from hugin.domain.state_machines import ensure_system_transition, ensure_task_transition
from hugin.domain.tasks import (
    ApplicationPolicyRecord,
    DuplicateTaskError,
    SystemState,
    SystemStateNotFoundError,
    SystemStateRecord,
    TaskNotFoundError,
    TaskRecord,
    TaskState,
)
from hugin.domain.time import as_utc
from hugin.domain.vacancies import VacancyAvailability

READY_STATES = (TaskState.PENDING, TaskState.RETRY_SCHEDULED)
ELIGIBILITY_CHECKED_STATES = (
    *READY_STATES,
    TaskState.REVIEW_REQUIRED,
    TaskState.INPUT_REQUIRED,
)
FORM_PREFLIGHT_RUNNING = "FORM_PREFLIGHT_RUNNING"
FORM_PREFLIGHT_INTERRUPTED = "FORM_PREFLIGHT_INTERRUPTED"
FORM_PREFLIGHT_PASSED = "FORM_PREFLIGHT_PASSED"


def _task_record(model: ApplicationTaskModel) -> TaskRecord:
    return TaskRecord(
        id=model.id,
        application_id=model.application_id,
        state=model.state,
        priority_score=model.priority_score,
        scheduled_at=as_utc(model.scheduled_at),
        attempts=model.attempts,
        last_error_code=model.last_error_code,
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _system_record(model: SystemStateModel) -> SystemStateRecord:
    return SystemStateRecord(
        state=model.state,
        next_apply_at=(as_utc(model.next_apply_at) if model.next_apply_at is not None else None),
        updated_at=as_utc(model.updated_at),
    )


def _policy_record(model: ApplicationSettingsModel) -> ApplicationPolicyRecord:
    return ApplicationPolicyRecord(
        timezone_name=model.timezone_name,
        daily_limit=model.hh_apply_daily_limit,
        delay_min_seconds=model.hh_apply_delay_min_seconds,
        delay_max_seconds=model.hh_apply_delay_max_seconds,
        updated_at=as_utc(model.updated_at),
    )


class QueueTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        application_id: int,
        priority_score: float,
        scheduled_at: datetime | None = None,
    ) -> TaskRecord:
        if not 0 <= priority_score <= 100:
            raise ValueError("priority_score must be between 0 and 100")

        existing_id = self._session.scalar(
            select(ApplicationTaskModel.id).where(
                ApplicationTaskModel.application_id == application_id
            )
        )
        if existing_id is not None:
            raise DuplicateTaskError(application_id)

        task = ApplicationTaskModel(
            application_id=application_id,
            state=TaskState.PENDING,
            priority_score=priority_score,
            scheduled_at=as_utc(scheduled_at or datetime.now(UTC)),
        )
        self._session.add(task)
        self._session.flush()
        return _task_record(task)

    def get(self, task_id: int) -> TaskRecord:
        task = self._session.get(ApplicationTaskModel, task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return _task_record(task)

    def get_by_application_id(self, application_id: int) -> TaskRecord | None:
        task = self._session.scalar(
            select(ApplicationTaskModel).where(
                ApplicationTaskModel.application_id == application_id
            )
        )
        return _task_record(task) if task is not None else None

    def claim_next(
        self,
        now: datetime | None = None,
        *,
        account_id: int | None = None,
        direction_id: int | None = None,
        require_ready_cover_letter: bool = False,
        exclude_ready_cover_letter: bool = False,
        cover_letter_instruction_version: str | None = None,
        vacancy_rules_version: str | None = None,
        vacancy_rule_categories: frozenset[str] | None = None,
        running_error_code: str | None = None,
    ) -> TaskRecord | None:
        if require_ready_cover_letter and exclude_ready_cover_letter:
            raise ValueError(
                "Нельзя одновременно требовать и исключать готовое сопроводительное письмо"
            )
        selected_at = as_utc(now or datetime.now(UTC))
        direction_priority = case(
            (
                CareerDirectionModel.scoring_config["role_scope"].as_string()
                == DirectionScope.PYTHON_BACKEND.value,
                0,
            ),
            else_=1,
        )
        location_priority = DirectionVacancyModel.rules_details["location_priority"].as_float()
        experience_priority = DirectionVacancyModel.rules_details["experience_priority"].as_float()
        statement = (
            select(ApplicationTaskModel.id)
            .join(ApplicationModel)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .outerjoin(
                CareerDirectionModel,
                CareerDirectionModel.id == ApplicationModel.direction_id,
            )
            .outerjoin(
                DirectionVacancyModel,
                and_(
                    DirectionVacancyModel.direction_id == ApplicationModel.direction_id,
                    DirectionVacancyModel.vacancy_id == ApplicationModel.vacancy_id,
                ),
            )
            .where(
                ApplicationTaskModel.state.in_(READY_STATES),
                ApplicationTaskModel.scheduled_at <= selected_at,
                ApplicationModel.state == ApplicationState.APPLYING,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                VacancyModel.duplicate_of_id.is_(None),
                or_(
                    ApplicationModel.direction_id.is_(None),
                    CareerDirectionModel.is_active.is_(True),
                ),
            )
            .order_by(
                direction_priority,
                location_priority.desc().nulls_last(),
                experience_priority.desc().nulls_last(),
                VacancyModel.published_at.desc().nulls_last(),
                ApplicationTaskModel.priority_score.desc(),
                ApplicationTaskModel.scheduled_at,
                ApplicationTaskModel.id,
            )
            .limit(1)
        )
        if account_id is not None:
            statement = statement.where(ApplicationModel.account_id == account_id)
        if direction_id is not None:
            statement = statement.where(ApplicationModel.direction_id == direction_id)
        if vacancy_rules_version is not None or vacancy_rule_categories is not None:
            statement = statement.where(DirectionVacancyModel.state == VacancyState.QUEUED)
        if vacancy_rules_version is not None:
            statement = statement.where(
                DirectionVacancyModel.rules_version == vacancy_rules_version
            )
        if vacancy_rule_categories is not None:
            statement = statement.where(
                DirectionVacancyModel.rules_details["category"]
                .as_string()
                .in_(tuple(vacancy_rule_categories))
            )
        if require_ready_cover_letter or exclude_ready_cover_letter:
            instruction_filter = (
                CoverLetterModel.instruction_version == cover_letter_instruction_version
                if cover_letter_instruction_version is not None
                else CoverLetterModel.instruction_version.startswith(
                    f"{CURRENT_COVER_LETTER_INSTRUCTION}_",
                    autoescape=True,
                )
            )
            ready_letter = (
                select(CoverLetterModel.id)
                .where(
                    CoverLetterModel.application_id == ApplicationModel.id,
                    CoverLetterModel.state == CoverLetterState.READY,
                    CoverLetterModel.text.is_not(None),
                    instruction_filter,
                )
                .exists()
            )
            statement = statement.where(
                ready_letter if require_ready_cover_letter else ~ready_letter
            )
        task_id = self._session.scalar(statement)
        if task_id is None:
            return None

        task = self._session.scalar(
            update(ApplicationTaskModel)
            .where(
                ApplicationTaskModel.id == task_id,
                ApplicationTaskModel.state.in_(READY_STATES),
                ApplicationTaskModel.application.has(
                    ApplicationModel.state == ApplicationState.APPLYING
                ),
            )
            .values(
                state=TaskState.RUNNING,
                attempts=ApplicationTaskModel.attempts + 1,
                last_error_code=running_error_code,
                updated_at=selected_at,
            )
            .returning(ApplicationTaskModel)
        )
        return _task_record(task) if task is not None else None

    def claim_exact(
        self,
        task_id: int,
        now: datetime | None = None,
        *,
        running_error_code: str | None = None,
    ) -> TaskRecord | None:
        selected_at = as_utc(now or datetime.now(UTC))
        task = self._session.scalar(
            update(ApplicationTaskModel)
            .where(
                ApplicationTaskModel.id == task_id,
                ApplicationTaskModel.state.in_(READY_STATES),
                ApplicationTaskModel.scheduled_at <= selected_at,
                ApplicationTaskModel.application.has(
                    ApplicationModel.state == ApplicationState.APPLYING
                ),
            )
            .values(
                state=TaskState.RUNNING,
                attempts=ApplicationTaskModel.attempts + 1,
                last_error_code=running_error_code,
                updated_at=selected_at,
            )
            .returning(ApplicationTaskModel)
        )
        return _task_record(task) if task is not None else None

    def requeue_after_rule_change(
        self,
        task_id: int,
        *,
        priority_score: float,
    ) -> TaskRecord:
        task = self._session.get(ApplicationTaskModel, task_id)
        if (
            task is None
            or task.state is not TaskState.SKIPPED
            or task.last_error_code != "VACANCY_RULES_CHANGED"
        ):
            raise ValueError("Задание не было остановлено изменением правил")
        task.state = TaskState.PENDING
        task.priority_score = priority_score
        task.scheduled_at = datetime.now(UTC)
        task.last_error_code = None
        self._session.flush()
        return _task_record(task)

    def requeue_after_cover_letter_change(self, task_id: int) -> TaskRecord:
        task = self._session.get(ApplicationTaskModel, task_id)
        if task is None or task.state not in {
            TaskState.REVIEW_REQUIRED,
            TaskState.SKIPPED,
        }:
            raise ValueError("Задание не было остановлено при подготовке письма")
        task.state = TaskState.RETRY_SCHEDULED
        task.scheduled_at = datetime.now(UTC)
        task.last_error_code = "COVER_LETTER_INSTRUCTION_CHANGED"
        self._session.flush()
        return _task_record(task)

    def skip_ineligible(
        self,
        direction_id: int,
        *,
        rules_version: str,
        allowed_categories: frozenset[str] | None = None,
    ) -> int:
        rules_ineligible = [
            DirectionVacancyModel.state != VacancyState.QUEUED,
            DirectionVacancyModel.rules_version != rules_version,
        ]
        if allowed_categories is not None:
            rules_ineligible.append(
                DirectionVacancyModel.rules_details["category"]
                .as_string()
                .not_in(tuple(allowed_categories))
            )
        tasks = tuple(
            self._session.execute(
                select(
                    ApplicationTaskModel.id,
                    VacancyModel.duplicate_of_id,
                )
                .join(
                    ApplicationModel,
                    ApplicationModel.id == ApplicationTaskModel.application_id,
                )
                .join(
                    VacancyModel,
                    VacancyModel.id == ApplicationModel.vacancy_id,
                )
                .join(
                    DirectionVacancyModel,
                    and_(
                        DirectionVacancyModel.direction_id == ApplicationModel.direction_id,
                        DirectionVacancyModel.vacancy_id == ApplicationModel.vacancy_id,
                    ),
                )
                .where(
                    ApplicationModel.direction_id == direction_id,
                    ApplicationTaskModel.state.in_(ELIGIBILITY_CHECKED_STATES),
                    or_(
                        *rules_ineligible,
                        VacancyModel.duplicate_of_id.is_not(None),
                    ),
                )
            )
        )
        for task_id, duplicate_of_id in tasks:
            self.transition(
                task_id,
                TaskState.SKIPPED,
                error_code=(
                    "VACANCY_DUPLICATE" if duplicate_of_id is not None else "VACANCY_RULES_CHANGED"
                ),
            )
        return len(tasks)

    def recover_running(
        self,
        *,
        recovery: str = "startup",
        now: datetime | None = None,
    ) -> list[TaskRecord]:
        selected_at = as_utc(now or datetime.now(UTC))
        tasks = self._session.execute(
            select(
                ApplicationTaskModel.id,
                ApplicationTaskModel.last_error_code,
                ApplicationTaskModel.application_id,
                VacancyModel.availability,
            )
            .join(
                ApplicationModel,
                ApplicationModel.id == ApplicationTaskModel.application_id,
            )
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(ApplicationTaskModel.state == TaskState.RUNNING)
            .order_by(ApplicationTaskModel.id)
        )
        recovered: list[TaskRecord] = []
        for task_id, error_code, application_id, availability in tasks:
            if error_code == FORM_PREFLIGHT_RUNNING:
                if availability is not VacancyAvailability.ACTIVE:
                    application = self._session.get(ApplicationModel, application_id)
                    if application is not None and application.state is ApplicationState.APPLYING:
                        application.state = ApplicationState.CLOSED
                        self._session.add(
                            ApplicationEventModel(
                                application_id=application_id,
                                event_type=ApplicationEventType.STATE_CHANGED,
                                payload={
                                    "previous_state": ApplicationState.APPLYING.value,
                                    "state": ApplicationState.CLOSED.value,
                                    "reason": f"VACANCY_{availability.value}",
                                },
                            )
                        )
                    recovered.append(
                        self.transition(
                            task_id,
                            TaskState.SKIPPED,
                            error_code=f"VACANCY_{availability.value}",
                        )
                    )
                    continue
                recovered.append(
                    self.transition(
                        task_id,
                        TaskState.RETRY_SCHEDULED,
                        scheduled_at=selected_at,
                        error_code=FORM_PREFLIGHT_INTERRUPTED,
                    )
                )
                continue
            recovered.append(
                self.transition(
                    task_id,
                    TaskState.UNKNOWN_RESULT,
                    error_code="INTERRUPTED_DURING_APPLY",
                    event_payload={"recovery": recovery},
                )
            )
        return recovered

    def count_by_state(self) -> dict[TaskState, int]:
        rows = self._session.execute(
            select(ApplicationTaskModel.state, func.count()).group_by(ApplicationTaskModel.state)
        )
        return {state: count for state, count in rows}

    def has_unknown_result(self) -> bool:
        return (
            self._session.scalar(
                select(ApplicationTaskModel.id)
                .where(ApplicationTaskModel.state == TaskState.UNKNOWN_RESULT)
                .limit(1)
            )
            is not None
        )

    def transition(
        self,
        task_id: int,
        target: TaskState,
        *,
        scheduled_at: datetime | None = None,
        error_code: str | None = None,
        event_payload: EventPayload | None = None,
    ) -> TaskRecord:
        task = self._session.get(ApplicationTaskModel, task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        ensure_task_transition(task.state, target)

        if target is TaskState.RETRY_SCHEDULED and scheduled_at is None:
            raise ValueError("scheduled_at is required for retry")

        task.state = target
        task.last_error_code = error_code
        if scheduled_at is not None:
            task.scheduled_at = as_utc(scheduled_at)
        if target is TaskState.UNKNOWN_RESULT:
            payload: EventPayload = dict(event_payload or {})
            payload.update({"task_id": task.id, "error_code": error_code})
            self._session.add(
                ApplicationEventModel(
                    application_id=task.application_id,
                    event_type=ApplicationEventType.UNKNOWN_RESULT,
                    payload=payload,
                )
            )
        self._session.flush()
        return _task_record(task)


class SystemStateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> SystemStateRecord:
        model = self._session.get(SystemStateModel, 1)
        if model is None:
            raise SystemStateNotFoundError
        return _system_record(model)

    def lock(self) -> SystemStateRecord:
        model = self._session.scalar(
            select(SystemStateModel)
            .where(SystemStateModel.id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if model is None:
            raise SystemStateNotFoundError
        return _system_record(model)

    def transition(self, target: SystemState) -> SystemStateRecord:
        model = self._session.scalar(
            select(SystemStateModel)
            .where(SystemStateModel.id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if model is None:
            raise SystemStateNotFoundError
        ensure_system_transition(model.state, target)
        if (
            target is SystemState.RUNNING
            and model.supervised_lease_token is not None
            and model.supervised_lease_expires_at is not None
            and as_utc(model.supervised_lease_expires_at) > datetime.now(UTC)
        ):
            raise ValueError(
                "Нельзя включить очередь, пока выполняется управляемый поштучный отклик"
            )
        if model.state in {SystemState.RUNNING, SystemState.PAUSED} and target in {
            SystemState.AUTH_REQUIRED,
            SystemState.CAPTCHA_REQUIRED,
            SystemState.ACCOUNT_WARNING,
        }:
            model.recovery_state = model.state
        elif target in {SystemState.RUNNING, SystemState.PAUSED}:
            model.recovery_state = None
        model.state = target
        self._session.flush()
        return _system_record(model)

    def resume_after_authentication(self) -> SystemStateRecord:
        model = self._session.scalar(
            select(SystemStateModel)
            .where(SystemStateModel.id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if model is None:
            raise SystemStateNotFoundError
        if model.state not in {SystemState.AUTH_REQUIRED, SystemState.CAPTCHA_REQUIRED}:
            return _system_record(model)
        target = (
            model.recovery_state
            if model.recovery_state in {SystemState.RUNNING, SystemState.PAUSED}
            else SystemState.RUNNING
        )
        ensure_system_transition(model.state, target)
        model.state = target
        model.recovery_state = None
        self._session.flush()
        return _system_record(model)

    def set_next_apply_at(self, value: datetime | None) -> SystemStateRecord:
        model = self._session.get(SystemStateModel, 1)
        if model is None:
            raise SystemStateNotFoundError
        model.next_apply_at = as_utc(value) if value is not None else None
        self._session.flush()
        return _system_record(model)

    def acquire_supervised_lease(
        self,
        token: str,
        *,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=15),
    ) -> datetime:
        if not token or len(token) > 64:
            raise ValueError("Некорректный идентификатор управляемого сеанса")
        selected_at = as_utc(now or datetime.now(UTC))
        if ttl <= timedelta(0):
            raise ValueError("Срок аренды должен быть положительным")
        expires_at = selected_at + ttl
        updated_id = self._session.scalar(
            update(SystemStateModel)
            .where(
                SystemStateModel.id == 1,
                SystemStateModel.state == SystemState.PAUSED,
                or_(
                    SystemStateModel.supervised_lease_token.is_(None),
                    SystemStateModel.supervised_lease_expires_at.is_(None),
                    SystemStateModel.supervised_lease_expires_at <= selected_at,
                    SystemStateModel.supervised_lease_token == token,
                ),
            )
            .values(
                supervised_lease_token=token,
                supervised_lease_expires_at=expires_at,
                updated_at=selected_at,
            )
            .returning(SystemStateModel.id)
        )
        if updated_id != 1:
            raise RuntimeError(
                "Очередь должна быть на паузе, а другой управляемый сеанс — завершён"  # noqa: RUF001
            )
        self._session.flush()
        return expires_at

    def supervised_lease_is_valid(
        self,
        token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        selected_at = as_utc(now or datetime.now(UTC))
        return bool(
            self._session.scalar(
                select(SystemStateModel.id).where(
                    SystemStateModel.id == 1,
                    SystemStateModel.state == SystemState.PAUSED,
                    SystemStateModel.supervised_lease_token == token,
                    SystemStateModel.supervised_lease_expires_at > selected_at,
                )
            )
        )

    def supervised_lease_active(self, now: datetime | None = None) -> bool:
        selected_at = as_utc(now or datetime.now(UTC))
        return bool(
            self._session.scalar(
                select(SystemStateModel.id).where(
                    SystemStateModel.id == 1,
                    SystemStateModel.supervised_lease_token.is_not(None),
                    SystemStateModel.supervised_lease_expires_at > selected_at,
                )
            )
        )

    def clear_expired_supervised_lease(self, now: datetime | None = None) -> bool:
        selected_at = as_utc(now or datetime.now(UTC))
        cleared_id = self._session.scalar(
            update(SystemStateModel)
            .where(
                SystemStateModel.id == 1,
                SystemStateModel.supervised_lease_token.is_not(None),
                or_(
                    SystemStateModel.supervised_lease_expires_at.is_(None),
                    SystemStateModel.supervised_lease_expires_at <= selected_at,
                ),
            )
            .values(
                supervised_lease_token=None,
                supervised_lease_expires_at=None,
                updated_at=selected_at,
            )
            .returning(SystemStateModel.id)
        )
        self._session.flush()
        return cleared_id == 1

    def release_supervised_lease(self, token: str) -> None:
        self._session.execute(
            update(SystemStateModel)
            .where(
                SystemStateModel.id == 1,
                SystemStateModel.supervised_lease_token == token,
            )
            .values(
                supervised_lease_token=None,
                supervised_lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
        )
        self._session.flush()


class ApplicationSettingsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> ApplicationPolicyRecord:
        model = self._session.get(ApplicationSettingsModel, 1)
        if model is None:
            raise LookupError("Настройки очереди не найдены")
        return _policy_record(model)

    def update(
        self,
        *,
        timezone_name: str | None = None,
        daily_limit: int | None = None,
        delay_min_seconds: int | None = None,
        delay_max_seconds: int | None = None,
    ) -> ApplicationPolicyRecord:
        model = self._session.get(ApplicationSettingsModel, 1)
        if model is None:
            raise LookupError("Настройки очереди не найдены")
        proposed_daily_limit = model.hh_apply_daily_limit if daily_limit is None else daily_limit
        proposed_delay_min = (
            model.hh_apply_delay_min_seconds if delay_min_seconds is None else delay_min_seconds
        )
        proposed_delay_max = (
            model.hh_apply_delay_max_seconds if delay_max_seconds is None else delay_max_seconds
        )
        if proposed_daily_limit < 25:
            raise ValueError("Дневное ограничение не может быть меньше 25")
        if proposed_delay_min < 0 or proposed_delay_max < proposed_delay_min:
            raise ValueError("Некорректный интервал между откликами")
        if timezone_name is not None:
            model.timezone_name = timezone_name
        model.hh_apply_daily_limit = proposed_daily_limit
        model.hh_apply_delay_min_seconds = proposed_delay_min
        model.hh_apply_delay_max_seconds = proposed_delay_max
        self._session.flush()
        return _policy_record(model)
