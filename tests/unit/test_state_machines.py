from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

import pytest

from hugin.domain import (
    ApplicationState,
    InvalidStateTransitionError,
    SystemState,
    TaskState,
    ensure_application_transition,
    ensure_system_transition,
    ensure_task_transition,
)


def test_valid_state_transitions() -> None:
    ensure_application_transition(ApplicationState.APPLYING, ApplicationState.APPLIED)
    ensure_task_transition(TaskState.RUNNING, TaskState.UNKNOWN_RESULT)
    ensure_task_transition(TaskState.RUNNING, TaskState.INPUT_REQUIRED)
    ensure_task_transition(TaskState.INPUT_REQUIRED, TaskState.REVIEW_REQUIRED)
    ensure_task_transition(TaskState.REVIEW_REQUIRED, TaskState.RETRY_SCHEDULED)
    ensure_system_transition(SystemState.RUNNING, SystemState.CAPTCHA_REQUIRED)


@pytest.mark.parametrize(
    ("current", "target", "transition"),
    [
        (ApplicationState.VIEWED, ApplicationState.APPLYING, ensure_application_transition),
        (TaskState.COMPLETED, TaskState.RUNNING, ensure_task_transition),
        (SystemState.ACCOUNT_WARNING, SystemState.CAPTCHA_REQUIRED, ensure_system_transition),
    ],
)
def test_invalid_state_transitions_are_rejected(
    current: StrEnum,
    target: StrEnum,
    transition: Callable[[StrEnum, StrEnum], None],
) -> None:
    with pytest.raises(InvalidStateTransitionError) as error:
        transition(current, target)

    assert error.value.current is current
    assert error.value.target is target
