"""Data repositories."""

from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.tasks import QueueTaskRepository, SystemStateRepository
from hugin.repositories.vacancies import VacancyRepository

__all__ = [
    "ApplicationRepository",
    "QueueTaskRepository",
    "SystemStateRepository",
    "VacancyRepository",
]
