from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import SystemStateModel
from hugin.domain import (
    ApplicationEventType,
    ApplicationNotFoundError,
    ApplicationState,
    DuplicateTaskError,
    InvalidStateTransitionError,
    SystemState,
    SystemStateNotFoundError,
    TaskNotFoundError,
    TaskState,
    VacancyData,
)
from hugin.repositories import (
    ApplicationRepository,
    QueueTaskRepository,
    SystemStateRepository,
    VacancyRepository,
)
from hugin.services import QueueService


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    selected = Settings(environment="test", data_dir=tmp_path)
    upgrade_database(selected)
    return selected


def create_application(session: Session, hh_id: str, resume_hh_id: str) -> int:
    vacancy = VacancyRepository(session).upsert(
        VacancyData(
            hh_id=hh_id,
            title=f"Vacancy {hh_id}",
            source_url=f"https://hh.ru/vacancy/{hh_id}",
        )
    )
    return ApplicationRepository(session).create_apply_intent(vacancy.id, resume_hh_id).id


def test_queue_respects_system_state_and_priority(settings: Settings) -> None:
    database = create_database(settings)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            low_id = create_application(session, "100", "resume-1")
            high_id = create_application(session, "200", "resume-2")
            tasks = QueueTaskRepository(session)
            tasks.enqueue(low_id, priority_score=20, scheduled_at=now)
            high = tasks.enqueue(high_id, priority_score=80, scheduled_at=now)

            system = SystemStateRepository(session)
            assert system.get().state is SystemState.RUNNING
            assert system.transition(SystemState.PAUSED).state is SystemState.PAUSED
            assert QueueService(session).claim_next(now) is None
            assert system.transition(SystemState.RUNNING).state is SystemState.RUNNING

            claimed = QueueService(session).claim_next(now)
            assert claimed is not None
            assert claimed.id == high.id
            assert claimed.state is TaskState.RUNNING
            assert claimed.attempts == 1
    finally:
        database.close()


def test_unknown_result_requires_reconciliation_before_retry(settings: Settings) -> None:
    database = create_database(settings)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            application_id = create_application(session, "300", "resume-1")
            repository = QueueTaskRepository(session)
            task = repository.enqueue(application_id, 50, now)
            claimed = repository.claim_next(now)
            assert claimed is not None
            assert claimed.id == task.id

            unknown = repository.transition(
                task.id,
                TaskState.UNKNOWN_RESULT,
                error_code="RESULT_NOT_CONFIRMED",
            )
            assert unknown.last_error_code == "RESULT_NOT_CONFIRMED"
            events = ApplicationRepository(session).list_events(application_id)
            assert events[-1].event_type is ApplicationEventType.UNKNOWN_RESULT
            assert events[-1].payload == {
                "task_id": task.id,
                "error_code": "RESULT_NOT_CONFIRMED",
            }

            with pytest.raises(InvalidStateTransitionError):
                repository.transition(task.id, TaskState.RUNNING)

            with pytest.raises(ValueError, match="scheduled_at"):
                repository.transition(task.id, TaskState.RETRY_SCHEDULED)

            retry_at = now + timedelta(minutes=15)
            retry = repository.transition(
                task.id,
                TaskState.RETRY_SCHEDULED,
                scheduled_at=retry_at,
            )
            assert retry.scheduled_at == retry_at
            assert repository.claim_next(now) is None

            second_attempt = repository.claim_next(retry_at)
            assert second_attempt is not None
            assert second_attempt.attempts == 2
            assert repository.transition(task.id, TaskState.COMPLETED).state is TaskState.COMPLETED

            with pytest.raises(InvalidStateTransitionError):
                repository.transition(task.id, TaskState.RUNNING)
    finally:
        database.close()


def test_missing_system_state_is_reported(settings: Settings) -> None:
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            model = session.get(SystemStateModel, 1)
            assert model is not None
            session.delete(model)
            session.flush()

            repository = SystemStateRepository(session)
            with pytest.raises(SystemStateNotFoundError):
                repository.get()
            with pytest.raises(SystemStateNotFoundError):
                repository.transition(SystemState.PAUSED)
    finally:
        database.close()


def test_application_transition_appends_event_and_rejects_invalid_path(
    settings: Settings,
) -> None:
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            application_id = create_application(session, "400", "resume-1")
            repository = ApplicationRepository(session)
            applied = repository.transition_state(
                application_id,
                ApplicationState.APPLIED,
                {"confirmation": "history"},
            )
            assert applied.state is ApplicationState.APPLIED
            viewed = repository.transition_state(application_id, ApplicationState.VIEWED)
            assert viewed.state is ApplicationState.VIEWED

            events = repository.list_events(application_id)
            assert [event.event_type for event in events] == [
                ApplicationEventType.APPLY_INTENT,
                ApplicationEventType.APPLIED,
                ApplicationEventType.STATE_CHANGED,
            ]
            assert events[1].payload == {
                "confirmation": "history",
                "previous_state": "APPLYING",
                "state": "APPLIED",
            }

            with pytest.raises(InvalidStateTransitionError):
                repository.transition_state(application_id, ApplicationState.QUEUED)
            with pytest.raises(ApplicationNotFoundError):
                repository.transition_state(-1, ApplicationState.APPLIED)
    finally:
        database.close()


def test_queue_rejects_invalid_or_duplicate_tasks(settings: Settings) -> None:
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            application_id = create_application(session, "500", "resume-1")
            repository = QueueTaskRepository(session)

            with pytest.raises(ValueError, match="priority_score"):
                repository.enqueue(application_id, 101)

            task = repository.enqueue(application_id, 10)
            assert repository.get(task.id) == task
            with pytest.raises(DuplicateTaskError):
                repository.enqueue(application_id, 20)
            with pytest.raises(TaskNotFoundError):
                repository.get(-1)
            with pytest.raises(TaskNotFoundError):
                repository.transition(-1, TaskState.SKIPPED)
    finally:
        database.close()
