from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    SystemStateModel,
)
from hugin.domain.applications import ApplicationEventType, EventPayload
from hugin.domain.state_machines import ensure_system_transition, ensure_task_transition
from hugin.domain.tasks import (
    DuplicateTaskError,
    SystemState,
    SystemStateNotFoundError,
    SystemStateRecord,
    TaskNotFoundError,
    TaskRecord,
    TaskState,
)
from hugin.domain.time import as_utc

READY_STATES = (TaskState.PENDING, TaskState.RETRY_SCHEDULED)


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
    return SystemStateRecord(state=model.state, updated_at=as_utc(model.updated_at))


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

    def claim_next(
        self,
        now: datetime | None = None,
        *,
        direction_id: int | None = None,
    ) -> TaskRecord | None:
        selected_at = as_utc(now or datetime.now(UTC))
        statement = (
            select(ApplicationTaskModel.id)
            .join(ApplicationModel)
            .where(
                ApplicationTaskModel.state.in_(READY_STATES),
                ApplicationTaskModel.scheduled_at <= selected_at,
            )
            .order_by(
                ApplicationTaskModel.priority_score.desc(),
                ApplicationTaskModel.scheduled_at,
                ApplicationTaskModel.id,
            )
            .limit(1)
        )
        if direction_id is not None:
            statement = statement.where(ApplicationModel.direction_id == direction_id)
        task_id = self._session.scalar(statement)
        if task_id is None:
            return None

        task = self._session.scalar(
            update(ApplicationTaskModel)
            .where(
                ApplicationTaskModel.id == task_id,
                ApplicationTaskModel.state.in_(READY_STATES),
            )
            .values(
                state=TaskState.RUNNING,
                attempts=ApplicationTaskModel.attempts + 1,
                updated_at=selected_at,
            )
            .returning(ApplicationTaskModel)
        )
        return _task_record(task) if task is not None else None

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

    def transition(self, target: SystemState) -> SystemStateRecord:
        model = self._session.get(SystemStateModel, 1)
        if model is None:
            raise SystemStateNotFoundError
        ensure_system_transition(model.state, target)
        model.state = target
        self._session.flush()
        return _system_record(model)
