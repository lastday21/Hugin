"""Domain model."""

from hugin.domain.applications import (
    ApplicationEventRecord,
    ApplicationEventType,
    ApplicationNotFoundError,
    ApplicationRecord,
    ApplicationState,
    DuplicateApplicationError,
    EventPayload,
)
from hugin.domain.directions import (
    AccountRecord,
    ConfigPayload,
    DirectionRecord,
    DirectionVacancyRecord,
    ResumeRecord,
    SearchQueryRecord,
    VacancyState,
)
from hugin.domain.hh import HhApplyResult, HhApplyStatus, HhResumeDetails
from hugin.domain.state_machines import (
    InvalidStateTransitionError,
    ensure_application_transition,
    ensure_system_transition,
    ensure_task_transition,
)
from hugin.domain.tasks import (
    DuplicateTaskError,
    SystemState,
    SystemStateNotFoundError,
    SystemStateRecord,
    TaskNotFoundError,
    TaskRecord,
    TaskState,
)
from hugin.domain.vacancies import VacancyData, VacancyRecord

__all__ = [
    "AccountRecord",
    "ApplicationEventRecord",
    "ApplicationEventType",
    "ApplicationNotFoundError",
    "ApplicationRecord",
    "ApplicationState",
    "ConfigPayload",
    "DirectionRecord",
    "DirectionVacancyRecord",
    "DuplicateApplicationError",
    "DuplicateTaskError",
    "EventPayload",
    "HhApplyResult",
    "HhApplyStatus",
    "HhResumeDetails",
    "InvalidStateTransitionError",
    "ResumeRecord",
    "SearchQueryRecord",
    "SystemState",
    "SystemStateNotFoundError",
    "SystemStateRecord",
    "TaskNotFoundError",
    "TaskRecord",
    "TaskState",
    "VacancyData",
    "VacancyRecord",
    "VacancyState",
    "ensure_application_transition",
    "ensure_system_transition",
    "ensure_task_transition",
]
