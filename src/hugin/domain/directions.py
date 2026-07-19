from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type ConfigPayload = dict[str, object]


class VacancyState(StrEnum):
    DISCOVERED = "DISCOVERED"
    FILTERED = "FILTERED"
    ANALYZED = "ANALYZED"
    QUEUED = "QUEUED"
    SKIPPED = "SKIPPED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class AccountRecord:
    id: int
    label: str
    external_id: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DirectionRecord:
    id: int
    account_id: int
    name: str
    description: str | None
    scoring_config: ConfigPayload
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SearchQueryRecord:
    id: int
    direction_id: int
    query: str
    area: str
    filters: ConfigPayload
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResumeRecord:
    id: int
    account_id: int
    hh_id: str
    title: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DirectionVacancyRecord:
    direction_id: int
    vacancy_id: int
    state: VacancyState
    rules_score: float | None
    ai_score: float | None
    fit_score: float | None
    first_seen_at: datetime
    updated_at: datetime
