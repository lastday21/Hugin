from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain import (
    ApplicationEventType,
    ApplicationReconciliationResult,
    ApplicationState,
    ReconciliationStatus,
    SystemState,
    TaskState,
    VacancyData,
)
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    QueueTaskRepository,
    ResumeRepository,
    SystemStateRepository,
    VacancyRepository,
)
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.application_reconciliation import ApplicationReconciliationService
from hugin.services.queue import QueueService

pytestmark = pytest.mark.integration


def create_unknown_task(session: Session, suffix: str) -> tuple[int, int]:
    account = AccountRepository(session).create(f"Аккаунт {suffix}")
    resume = ResumeRepository(session).upsert(account.id, f"resume-{suffix}", "Python")
    vacancy = VacancyRepository(session).upsert(
        VacancyData(
            f"reconciliation-{suffix}",
            "Python разработчик",
            f"https://hh.ru/vacancy/reconciliation-{suffix}",
        )
    )
    application = ApplicationRepository(session).create_apply_intent(
        account.id,
        vacancy.id,
        resume.id,
    )
    tasks = QueueTaskRepository(session)
    task = tasks.enqueue(application.id, 50)
    claimed = tasks.claim_next()
    assert claimed is not None
    tasks.transition(
        task.id,
        TaskState.UNKNOWN_RESULT,
        error_code="RESULT_NOT_CONFIRMED",
    )
    SystemStateRepository(session).transition(SystemState.PAUSED)
    return application.id, task.id


def test_applied_reconciliation_completes_task_and_writes_event(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    checked_at = datetime(2026, 7, 26, 12, 30, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            application_id, task_id = create_unknown_task(session, "applied")

            outcome = ApplicationReconciliationService(session).reconcile(
                task_id,
                ApplicationReconciliationResult(
                    ReconciliationStatus.APPLIED,
                    final_url="https://hh.ru/applicant/negotiations",
                    confirmation="Отклик найден",
                    checked_at=checked_at,
                ),
            )

            assert not outcome.blocking
            assert outcome.application.state is ApplicationState.APPLIED
            assert outcome.task.state is TaskState.COMPLETED
            assert SystemStateRepository(session).get().state is SystemState.PAUSED

            events = ApplicationRepository(session).list_events(application_id)
            reconciliation = [
                event for event in events if event.payload.get("action") == "RESULT_RECONCILED"
            ]
            assert len(reconciliation) == 1
            assert reconciliation[0].event_type is ApplicationEventType.STATE_CHANGED
            assert reconciliation[0].payload["reconciliation_status"] == "APPLIED"
            assert reconciliation[0].payload["checked_at"] == checked_at.isoformat()
            assert events[-1].event_type is ApplicationEventType.APPLIED

            with pytest.raises(ValueError, match="неизвестном результате"):
                ApplicationReconciliationService(session).reconcile(
                    task_id,
                    ApplicationReconciliationResult(ReconciliationStatus.APPLIED),
                )
    finally:
        database.close()


def test_not_found_reconciliation_requires_review_before_retry(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            application_id, task_id = create_unknown_task(session, "not-found")

            outcome = ApplicationReconciliationService(session).reconcile(
                task_id,
                ApplicationReconciliationResult(
                    ReconciliationStatus.NOT_FOUND,
                    confirmation="Отклик не найден",
                ),
            )

            assert not outcome.blocking
            assert outcome.application.state is ApplicationState.APPLYING
            assert outcome.task.state is TaskState.REVIEW_REQUIRED
            assert outcome.task.last_error_code == "RECONCILED_NOT_FOUND"
            assert QueueService(session).resume().state is SystemState.RUNNING
            event = ApplicationRepository(session).list_events(application_id)[-1]
            assert event.payload["reconciliation_status"] == "NOT_FOUND"
    finally:
        database.close()


@pytest.mark.parametrize(
    ("status", "expected_system"),
    [
        (ReconciliationStatus.AUTH_REQUIRED, SystemState.AUTH_REQUIRED),
        (ReconciliationStatus.CAPTCHA_REQUIRED, SystemState.CAPTCHA_REQUIRED),
        (ReconciliationStatus.UNAVAILABLE, SystemState.PAUSED),
    ],
)
def test_unresolved_reconciliation_keeps_unknown_result_blocking(
    settings: Settings,
    status: ReconciliationStatus,
    expected_system: SystemState,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            application_id, task_id = create_unknown_task(session, status.value.lower())

            outcome = ApplicationReconciliationService(session).reconcile(
                task_id,
                ApplicationReconciliationResult(status),
            )

            assert outcome.blocking
            assert outcome.application.state is ApplicationState.APPLYING
            assert outcome.task.state is TaskState.UNKNOWN_RESULT
            assert SystemStateRepository(session).get().state is expected_system
            event = ApplicationRepository(session).list_events(application_id)[-1]
            assert event.event_type is ApplicationEventType.STATE_CHANGED
            assert event.payload["reconciliation_status"] == status.value

            ApplicationAutomationService(session).resume_after_authentication()
            assert SystemStateRepository(session).get().state is SystemState.PAUSED
            with pytest.raises(ValueError, match="неизвестен"):
                QueueService(session).resume()
    finally:
        database.close()
