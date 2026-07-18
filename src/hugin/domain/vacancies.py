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


@dataclass(frozen=True, slots=True)
class VacancyRecord:
    id: int
    hh_id: str
    title: str
    source_url: str
    employer_name: str | None
    published_at: datetime | None
    created_at: datetime
