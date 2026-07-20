from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
