"""Data repositories."""

from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.directions import AccountRepository, DirectionRepository, ResumeRepository
from hugin.repositories.tasks import (
    ApplicationSettingsRepository,
    QueueTaskRepository,
    SystemStateRepository,
)
from hugin.repositories.vacancies import VacancyRepository

__all__ = [
    "AccountRepository",
    "ApplicationRepository",
    "ApplicationSettingsRepository",
    "DirectionRepository",
    "QueueTaskRepository",
    "ResumeRepository",
    "SystemStateRepository",
    "VacancyRepository",
]
