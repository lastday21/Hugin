"""Data repositories."""

from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.directions import AccountRepository, DirectionRepository, ResumeRepository
from hugin.repositories.tasks import QueueTaskRepository, SystemStateRepository
from hugin.repositories.vacancies import VacancyRepository

__all__ = [
    "AccountRepository",
    "ApplicationRepository",
    "DirectionRepository",
    "QueueTaskRepository",
    "ResumeRepository",
    "SystemStateRepository",
    "VacancyRepository",
]
