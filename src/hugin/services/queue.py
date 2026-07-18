from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from hugin.domain.tasks import SystemState, TaskRecord
from hugin.repositories.tasks import QueueTaskRepository, SystemStateRepository


class QueueService:
    def __init__(self, session: Session) -> None:
        self._tasks = QueueTaskRepository(session)
        self._system = SystemStateRepository(session)

    def claim_next(self, now: datetime | None = None) -> TaskRecord | None:
        if self._system.get().state is not SystemState.RUNNING:
            return None
        return self._tasks.claim_next(now or datetime.now(UTC))
