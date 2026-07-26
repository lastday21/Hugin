from __future__ import annotations

import pytest

from hugin.domain import (
    AutomationJobKind,
    AutomationJobNotFoundError,
    AutomationJobState,
    AutomationJobStateError,
    automation_job_key,
)


def test_automation_job_keys_reject_ambiguous_identifiers() -> None:
    assert automation_job_key(AutomationJobKind.SEARCH, 1, 7) == "search:7"
    assert automation_job_key(AutomationJobKind.MESSAGES, 2) == "messages:2"
    assert automation_job_key(AutomationJobKind.STATUSES, 3) == "statuses:3"

    with pytest.raises(ValueError, match="аккаунта"):
        automation_job_key(AutomationJobKind.MESSAGES, 0)
    with pytest.raises(ValueError, match="поискового запроса"):
        automation_job_key(AutomationJobKind.SEARCH, 1)
    with pytest.raises(ValueError, match="только для поиска"):
        automation_job_key(AutomationJobKind.STATUSES, 1, 4)


def test_automation_errors_keep_job_context() -> None:
    missing = AutomationJobNotFoundError("messages:1")
    assert missing.job_key == "messages:1"
    assert "messages:1" in str(missing)

    wrong_state = AutomationJobStateError(
        "search:7",
        AutomationJobState.BLOCKED,
        AutomationJobState.RUNNING,
    )
    assert wrong_state.job_key == "search:7"
    assert wrong_state.current is AutomationJobState.BLOCKED
    assert wrong_state.expected is AutomationJobState.RUNNING
    assert "BLOCKED" in str(wrong_state)
