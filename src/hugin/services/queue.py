from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from hugin.domain.tasks import (
    ApplicationPolicyRecord,
    SystemState,
    SystemStateRecord,
    TaskRecord,
    TaskState,
)
from hugin.repositories.tasks import (
    ApplicationSettingsRepository,
    QueueTaskRepository,
    SystemStateRepository,
)


@dataclass(frozen=True, slots=True)
class QueueStatus:
    policy: ApplicationPolicyRecord
    system: SystemStateRecord
    task_counts: dict[TaskState, int]


class QueueService:
    def __init__(self, session: Session) -> None:
        self._tasks = QueueTaskRepository(session)
        self._system = SystemStateRepository(session)
        self._settings = ApplicationSettingsRepository(session)

    def claim_next(
        self,
        now: datetime | None = None,
        *,
        direction_id: int | None = None,
    ) -> TaskRecord | None:
        selected_at = now or datetime.now(UTC)
        system = self._system.get()
        if system.state is not SystemState.RUNNING:
            return None
        if system.next_apply_at is not None and system.next_apply_at > selected_at:
            return None
        return self._tasks.claim_next(
            selected_at,
            direction_id=direction_id,
        )

    def policy(self, timezone_name: str | None = None) -> ApplicationPolicyRecord:
        if timezone_name is not None:
            return self._settings.update(timezone_name=timezone_name)
        return self._settings.get()

    def configure(
        self,
        *,
        timezone_name: str,
        daily_limit: int | None = None,
        delay_min_seconds: int | None = None,
        delay_max_seconds: int | None = None,
    ) -> ApplicationPolicyRecord:
        return self._settings.update(
            timezone_name=timezone_name,
            daily_limit=daily_limit,
            delay_min_seconds=delay_min_seconds,
            delay_max_seconds=delay_max_seconds,
        )

    def pause(self) -> SystemStateRecord:
        current = self._system.get()
        if current.state is SystemState.PAUSED:
            return current
        if current.state not in {SystemState.RUNNING, SystemState.ACCOUNT_WARNING}:
            raise ValueError("Работа уже остановлена защитным состоянием hh.ru")
        return self._system.transition(SystemState.PAUSED)

    def resume(self) -> SystemStateRecord:
        current = self._system.get()
        if current.state is SystemState.RUNNING:
            return current
        if current.state is not SystemState.PAUSED:
            raise ValueError("Сначала нужно устранить защитное состояние hh.ru")
        return self._system.transition(SystemState.RUNNING)

    def status(self) -> QueueStatus:
        return QueueStatus(
            policy=self._settings.get(),
            system=self._system.get(),
            task_counts=self._tasks.count_by_state(),
        )
