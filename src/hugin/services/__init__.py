"""Application services."""

from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.cover_letter import CoverLetterBuilder
from hugin.services.hh_profile import HhProfileSyncService
from hugin.services.job_search import JobSearchSyncService
from hugin.services.queue import QueueService
from hugin.services.vacancy_analysis import (
    PythonBackendRules,
    RuleCategory,
    VacancyAnalysisService,
)

__all__ = [
    "ApplicationAutomationService",
    "CoverLetterBuilder",
    "HhProfileSyncService",
    "JobSearchSyncService",
    "PythonBackendRules",
    "QueueService",
    "RuleCategory",
    "VacancyAnalysisService",
]
