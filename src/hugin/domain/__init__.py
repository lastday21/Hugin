"""Domain model."""

from hugin.domain.applications import (
    ApplicationEventRecord,
    ApplicationEventType,
    ApplicationRecord,
    ApplicationState,
    DuplicateApplicationError,
    EventPayload,
)
from hugin.domain.vacancies import VacancyData, VacancyRecord

__all__ = [
    "ApplicationEventRecord",
    "ApplicationEventType",
    "ApplicationRecord",
    "ApplicationState",
    "DuplicateApplicationError",
    "EventPayload",
    "VacancyData",
    "VacancyRecord",
]
