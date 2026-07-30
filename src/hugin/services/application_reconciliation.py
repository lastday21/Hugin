from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from hugin.domain.applications import (
    ApplicationEventType,
    ApplicationReconciliationResult,
    ApplicationRecord,
    ApplicationState,
    EventPayload,
    ReconciliationStatus,
)
from hugin.domain.tasks import SystemState, TaskRecord, TaskState
from hugin.domain.time import as_utc
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.tasks import QueueTaskRepository, SystemStateRepository


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    application: ApplicationRecord
    task: TaskRecord
    blocking: bool


class ApplicationReconciliationService:
    def __init__(self, session: Session) -> None:
        self._applications = ApplicationRepository(session)
        self._tasks = QueueTaskRepository(session)
        self._system = SystemStateRepository(session)

    def reconcile(
        self,
        task_id: int,
        result: ApplicationReconciliationResult,
    ) -> ReconciliationOutcome:
        task = self._tasks.get(task_id)
        if task.state is not TaskState.UNKNOWN_RESULT:
            raise ValueError("Сверка доступна только при неизвестном результате отклика")

        application = self._applications.get(task.application_id)
        payload: EventPayload = {
            "action": "RESULT_RECONCILED",
            "task_id": task.id,
            "reconciliation_status": result.status.value,
            "checked_at": as_utc(result.checked_at).isoformat(),
            "confirmation": result.confirmation[:1000],
            "final_url": result.final_url[:1000],
        }
        self._applications.append_event(
            application.id,
            ApplicationEventType.STATE_CHANGED,
            payload,
        )

        if result.status is ReconciliationStatus.APPLIED:
            application = self._applications.transition_state(
                application.id,
                ApplicationState.APPLIED,
                {
                    "hh_status": ReconciliationStatus.APPLIED.value,
                    "source": "hugin_reconciliation",
                    "reconciliation_status": result.status.value,
                    "checked_at": payload["checked_at"],
                    "confirmation": payload["confirmation"],
                    "final_url": payload["final_url"],
                },
            )
            task = self._tasks.transition(task.id, TaskState.COMPLETED)
            return ReconciliationOutcome(
                application,
                task,
                blocking=self._tasks.has_unknown_result(),
            )

        if result.status is ReconciliationStatus.NOT_FOUND:
            task = self._tasks.transition(
                task.id,
                TaskState.REVIEW_REQUIRED,
                error_code="RECONCILED_NOT_FOUND",
            )
            return ReconciliationOutcome(
                application,
                task,
                blocking=self._tasks.has_unknown_result(),
            )

        if result.status is ReconciliationStatus.AUTH_REQUIRED:
            self._protect_system(SystemState.AUTH_REQUIRED)
        elif result.status is ReconciliationStatus.CAPTCHA_REQUIRED:
            self._protect_system(SystemState.CAPTCHA_REQUIRED)

        return ReconciliationOutcome(application, task, blocking=True)

    def _protect_system(self, target: SystemState) -> None:
        current = self._system.get().state
        if current is target:
            return
        if current in {SystemState.RUNNING, SystemState.PAUSED}:
            self._system.transition(target)
