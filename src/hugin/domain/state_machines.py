from __future__ import annotations

from enum import StrEnum

from hugin.domain.applications import ApplicationState
from hugin.domain.tasks import SystemState, TaskState

APPLICATION_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.APPLYING: frozenset({ApplicationState.APPLIED, ApplicationState.CLOSED}),
    ApplicationState.APPLIED: frozenset(
        {
            ApplicationState.VIEWED,
            ApplicationState.INVITED,
            ApplicationState.REJECTED,
            ApplicationState.CLOSED,
        }
    ),
    ApplicationState.VIEWED: frozenset(
        {ApplicationState.INVITED, ApplicationState.REJECTED, ApplicationState.CLOSED}
    ),
    ApplicationState.INVITED: frozenset({ApplicationState.CLOSED}),
    ApplicationState.REJECTED: frozenset({ApplicationState.CLOSED}),
    ApplicationState.CLOSED: frozenset(),
}

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.RUNNING, TaskState.SKIPPED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.RETRY_SCHEDULED,
            TaskState.REVIEW_REQUIRED,
            TaskState.INPUT_REQUIRED,
            TaskState.SKIPPED,
            TaskState.UNKNOWN_RESULT,
        }
    ),
    TaskState.RETRY_SCHEDULED: frozenset({TaskState.RUNNING, TaskState.SKIPPED}),
    TaskState.REVIEW_REQUIRED: frozenset({TaskState.RETRY_SCHEDULED, TaskState.SKIPPED}),
    TaskState.INPUT_REQUIRED: frozenset({TaskState.REVIEW_REQUIRED, TaskState.SKIPPED}),
    TaskState.UNKNOWN_RESULT: frozenset(
        {TaskState.COMPLETED, TaskState.RETRY_SCHEDULED, TaskState.SKIPPED}
    ),
    TaskState.SKIPPED: frozenset(),
    TaskState.COMPLETED: frozenset(),
}

SYSTEM_TRANSITIONS: dict[SystemState, frozenset[SystemState]] = {
    SystemState.RUNNING: frozenset(
        {
            SystemState.PAUSED,
            SystemState.AUTH_REQUIRED,
            SystemState.CAPTCHA_REQUIRED,
            SystemState.ACCOUNT_WARNING,
        }
    ),
    SystemState.PAUSED: frozenset({SystemState.RUNNING}),
    SystemState.AUTH_REQUIRED: frozenset({SystemState.RUNNING}),
    SystemState.CAPTCHA_REQUIRED: frozenset({SystemState.RUNNING}),
    SystemState.ACCOUNT_WARNING: frozenset({SystemState.RUNNING, SystemState.PAUSED}),
}


class InvalidStateTransitionError(ValueError):
    def __init__(self, current: StrEnum, target: StrEnum) -> None:
        super().__init__(f"Transition from {current.value} to {target.value} is not allowed")
        self.current = current
        self.target = target


def _ensure_transition[State: StrEnum](
    current: State,
    target: State,
    transitions: dict[State, frozenset[State]],
) -> None:
    if target not in transitions[current]:
        raise InvalidStateTransitionError(current, target)


def ensure_application_transition(
    current: ApplicationState,
    target: ApplicationState,
) -> None:
    _ensure_transition(current, target, APPLICATION_TRANSITIONS)


def ensure_task_transition(current: TaskState, target: TaskState) -> None:
    _ensure_transition(current, target, TASK_TRANSITIONS)


def ensure_system_transition(current: SystemState, target: SystemState) -> None:
    _ensure_transition(current, target, SYSTEM_TRANSITIONS)
