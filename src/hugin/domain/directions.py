from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type ConfigPayload = dict[str, object]


class VacancyState(StrEnum):
    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    QUEUED = "QUEUED"
    FILTERED_OUT = "FILTERED_OUT"
    SKIPPED = "SKIPPED"
    CLOSED = "CLOSED"


class WorkFormat(StrEnum):
    REMOTE = "REMOTE"
    ON_SITE = "ON_SITE"
    HYBRID = "HYBRID"


class EmploymentForm(StrEnum):
    FULL = "FULL"
    PART = "PART"
    PROJECT = "PROJECT"
    FLY_IN_FLY_OUT = "FLY_IN_FLY_OUT"


@dataclass(frozen=True, slots=True)
class SearchRegion:
    area: str
    name: str


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
    regions: tuple[SearchRegion, ...]
    work_formats: tuple[WorkFormat, ...]
    schedule_minutes: int
    last_run_at: datetime | None
    next_run_at: datetime | None
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
    rules_details: ConfigPayload
    ai_score: float | None
    fit_score: float | None
    rules_version: str | None
    analyzed_at: datetime | None
    first_seen_at: datetime
    updated_at: datetime
