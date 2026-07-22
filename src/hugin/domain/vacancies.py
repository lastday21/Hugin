from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class VacancyAvailability(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class VacancyData:
    hh_id: str
    title: str
    source_url: str
    employer_name: str | None = None
    published_at: datetime | None = None
    description: str | None = None
    experience: str | None = None
    employment: str | None = None
    work_format: str | None = None
    key_skills: tuple[str, ...] = ()
    details_fetched_at: datetime | None = None
    region: str | None = None
    address: str | None = None
    salary_from: Decimal | None = None
    salary_to: Decimal | None = None
    salary_currency: str | None = None
    salary_gross: bool | None = None
    schedule: str | None = None
    responsibilities: str | None = None
    required_qualifications: str | None = None
    preferred_qualifications: str | None = None
    has_cover_letter: bool = False
    has_screening_form: bool = False
    has_external_link: bool = False
    has_test_assignment: bool = False
    availability: VacancyAvailability = VacancyAvailability.ACTIVE


@dataclass(frozen=True, slots=True)
class VacancyRecord:
    id: int
    hh_id: str
    title: str
    source_url: str
    employer_name: str | None
    published_at: datetime | None
    description: str | None
    experience: str | None
    employment: str | None
    work_format: str | None
    key_skills: tuple[str, ...]
    details_fetched_at: datetime | None
    created_at: datetime
    region: str | None = None
    address: str | None = None
    salary_from: Decimal | None = None
    salary_to: Decimal | None = None
    salary_currency: str | None = None
    salary_gross: bool | None = None
    schedule: str | None = None
    responsibilities: str | None = None
    required_qualifications: str | None = None
    preferred_qualifications: str | None = None
    has_cover_letter: bool = False
    has_screening_form: bool = False
    has_external_link: bool = False
    has_test_assignment: bool = False
    availability: VacancyAvailability = VacancyAvailability.ACTIVE
    duplicate_of_id: int | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VacancyChangeRecord:
    id: int
    vacancy_id: int
    event_type: str
    changes: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VacancyDiscoveryRecord:
    id: int
    vacancy_id: int
    direction_id: int | None
    search_query_id: int | None
    query_text: str
    region: str
    discovered_at: datetime


@dataclass(frozen=True, slots=True)
class VacancySearchResult:
    found: int
    vacancies: tuple[VacancyData, ...]

    def __post_init__(self) -> None:
        if self.found < 0:
            raise ValueError("found must not be negative")
        vacancy_ids = [vacancy.hh_id for vacancy in self.vacancies]
        if len(vacancy_ids) != len(set(vacancy_ids)):
            raise ValueError("vacancy search returned duplicate identifiers")
