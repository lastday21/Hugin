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
    "ApplicationEventRecord",
    "ApplicationEventType",
    "ApplicationNotFoundError",
    "ApplicationRecord",
    "ApplicationState",
    "DuplicateApplicationError",
    "DuplicateTaskError",
    "EventPayload",
    "InvalidStateTransitionError",
    "SystemState",
    "SystemStateNotFoundError",
    "SystemStateRecord",
    "TaskNotFoundError",
    "TaskRecord",
    "TaskState",
    "VacancyData",
    "VacancyRecord",
    "ensure_application_transition",
    "ensure_system_transition",
    "ensure_task_transition",
]
