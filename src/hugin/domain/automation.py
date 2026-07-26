from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type AutomationJobResult = dict[str, str | int | float | bool | None]


class AutomationJobKind(StrEnum):
    SEARCH = "SEARCH"
    MESSAGES = "MESSAGES"
    STATUSES = "STATUSES"


class AutomationJobState(StrEnum):
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class AutomationJobRecord:
    key: str
    kind: AutomationJobKind
    state: AutomationJobState
    account_id: int
    search_query_id: int | None
    interval_seconds: int
    next_run_at: datetime | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_success_at: datetime | None
    heartbeat_at: datetime | None
    consecutive_failures: int
    last_error_code: str | None
    last_error_message: str | None
    last_result: AutomationJobResult
    created_at: datetime
    updated_at: datetime


class AutomationJobNotFoundError(LookupError):
    def __init__(self, job_key: str) -> None:
        super().__init__(f"Фоновое задание «{job_key}» не найдено")
        self.job_key = job_key


class AutomationJobStateError(ValueError):
    def __init__(
        self,
        job_key: str,
        current: AutomationJobState,
        expected: AutomationJobState,
    ) -> None:
        super().__init__(
            f"Фоновое задание «{job_key}» находится в состоянии "
            f"{current.value}, ожидалось {expected.value}"
        )
        self.job_key = job_key
        self.current = current
        self.expected = expected


def automation_job_key(
    kind: AutomationJobKind,
    account_id: int,
    search_query_id: int | None = None,
) -> str:
    if account_id < 1:
        raise ValueError("Идентификатор аккаунта должен быть положительным")
    if kind is AutomationJobKind.SEARCH:
        if search_query_id is None or search_query_id < 1:
            raise ValueError("Для поиска нужен идентификатор поискового запроса")
        return f"search:{search_query_id}"
    if search_query_id is not None:
        raise ValueError("Идентификатор поискового запроса допустим только для поиска")
    return f"{kind.value.lower()}:{account_id}"
