from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type EventPayload = dict[str, str | int | float | bool | None]


class ApplicationState(StrEnum):
    DISCOVERED = "DISCOVERED"
    FILTERED = "FILTERED"
    ANALYZED = "ANALYZED"
    QUEUED = "QUEUED"
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


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    id: int
    vacancy_id: int
    resume_hh_id: str
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
    def __init__(self, vacancy_id: int) -> None:
        super().__init__(f"Application for vacancy {vacancy_id} already exists")
        self.vacancy_id = vacancy_id
