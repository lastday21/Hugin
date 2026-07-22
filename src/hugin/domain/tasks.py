from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    SKIPPED = "SKIPPED"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"
    COMPLETED = "COMPLETED"


class SystemState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    ACCOUNT_WARNING = "ACCOUNT_WARNING"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: int
    application_id: int
    state: TaskState
    priority_score: float
    scheduled_at: datetime
    attempts: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SystemStateRecord:
    state: SystemState
    next_apply_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ApplicationPolicyRecord:
    timezone_name: str
    daily_limit: int
    delay_min_seconds: int
    delay_max_seconds: int
    updated_at: datetime


class DuplicateTaskError(ValueError):
    def __init__(self, application_id: int) -> None:
        super().__init__(f"Task for application {application_id} already exists")
        self.application_id = application_id


class TaskNotFoundError(LookupError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"Task {task_id} was not found")
        self.task_id = task_id


class SystemStateNotFoundError(LookupError):
    pass
