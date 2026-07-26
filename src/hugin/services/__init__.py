"""Application services."""

from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.application_reconciliation import ApplicationReconciliationService
from hugin.services.automation import AutomationSchedulerService
from hugin.services.cover_letter import CoverLetterService
from hugin.services.hh_profile import HhProfileSyncService
from hugin.services.job_search import JobSearchSyncService
from hugin.services.queue import QueueService
from hugin.services.vacancy_analysis import (
    AdjacentItRules,
    PythonBackendRules,
    RuleCategory,
    VacancyAnalysisService,
)
from hugin.services.vacancy_review import VacancyReviewService

__all__ = [
    "AdjacentItRules",
    "ApplicationAutomationService",
    "ApplicationReconciliationService",
    "AutomationSchedulerService",
    "CoverLetterService",
    "HhProfileSyncService",
    "JobSearchSyncService",
    "PythonBackendRules",
    "QueueService",
    "RuleCategory",
    "VacancyAnalysisService",
    "VacancyReviewService",
]
