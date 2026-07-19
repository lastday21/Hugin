from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type EventPayload = dict[str, str | int | float | bool | None]


class ApplicationState(StrEnum):
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    VIEWED = "VIEWED"
    INVITED = "INVITED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class ApplicationEventType(StrEnum):
    APPLY_INTENT = "APPLY_INTENT"
    APPLIED = "APPLIED"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"
    STATE_CHANGED = "STATE_CHANGED"


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    id: int
    account_id: int
    vacancy_id: int
    resume_id: int
    direction_id: int | None
    state: ApplicationState
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ApplicationEventRecord:
    id: int
    application_id: int
    event_type: ApplicationEventType
    payload: EventPayload
    created_at: datetime


class DuplicateApplicationError(ValueError):
    def __init__(self, account_id: int, vacancy_id: int, resume_id: int) -> None:
        super().__init__(
            f"Application for account {account_id}, vacancy {vacancy_id} "
            f"and resume {resume_id} already exists"
        )
        self.account_id = account_id
        self.vacancy_id = vacancy_id
        self.resume_id = resume_id


class ApplicationNotFoundError(LookupError):
    def __init__(self, application_id: int) -> None:
        super().__init__(f"Application {application_id} was not found")
        self.application_id = application_id
