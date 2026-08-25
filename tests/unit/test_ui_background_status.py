from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from sqlalchemy.orm import Session

from hugin.domain import AutomationJobKind, AutomationJobState
from hugin.services.ui_workspace import UiWorkspaceService, _queue_error_text


def test_queue_hides_success_code_and_explains_retry_in_russian() -> None:
    assert _queue_error_text("FORM_PREFLIGHT_PASSED") is None
    assert _queue_error_text("RETRYABLE_ERROR") == (
        "hh.ru не открыл форму отклика; повтор запланирован"
    )


class FakeSession:
    def __init__(self, jobs: tuple[SimpleNamespace, ...]) -> None:
        self._jobs = jobs

    def scalars(self, _statement: object) -> tuple[SimpleNamespace, ...]:
        return self._jobs


def job(
    key: str,
    kind: AutomationJobKind,
    state: AutomationJobState,
    *,
    heartbeat_at: datetime,
    next_run_at: datetime,
) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        kind=kind,
        state=state,
        heartbeat_at=heartbeat_at,
        last_started_at=heartbeat_at,
        updated_at=heartbeat_at,
        next_run_at=next_run_at,
        last_success_at=None,
        last_error_message=None,
    )


def background_status(*jobs: SimpleNamespace) -> str:
    session = cast(Session, FakeSession(tuple(jobs)))
    return UiWorkspaceService(session)._background_status(1).state


def search_status(*jobs: SimpleNamespace) -> tuple[str, datetime | None]:
    session = cast(Session, FakeSession(tuple(jobs)))
    status = UiWorkspaceService(session)._background_status(1)
    return status.search_state, status.next_search_at


def test_fresh_running_job_keeps_background_active_while_another_job_waits() -> None:
    now = datetime.now(UTC)

    assert (
        background_status(
            job(
                "search:1",
                AutomationJobKind.SEARCH,
                AutomationJobState.RUNNING,
                heartbeat_at=now - timedelta(seconds=30),
                next_run_at=now - timedelta(minutes=4),
            ),
            job(
                "messages:1",
                AutomationJobKind.MESSAGES,
                AutomationJobState.WAITING,
                heartbeat_at=now - timedelta(minutes=5),
                next_run_at=now - timedelta(minutes=3),
            ),
        )
        == "RUNNING"
    )


def test_running_search_does_not_expose_its_old_start_time_as_the_next_search() -> None:
    now = datetime.now(UTC)

    assert search_status(
        job(
            "search:1",
            AutomationJobKind.SEARCH,
            AutomationJobState.RUNNING,
            heartbeat_at=now - timedelta(seconds=30),
            next_run_at=now - timedelta(days=1),
        )
    ) == ("RUNNING", None)


def test_overdue_search_is_reported_as_waiting_without_a_past_next_time() -> None:
    now = datetime.now(UTC)

    assert search_status(
        job(
            "search:1",
            AutomationJobKind.SEARCH,
            AutomationJobState.WAITING,
            heartbeat_at=now - timedelta(minutes=1),
            next_run_at=now - timedelta(days=1),
        )
    ) == ("WAITING", None)


def test_stale_running_job_is_reported_as_stopped() -> None:
    now = datetime.now(UTC)

    assert (
        background_status(
            job(
                "search:1",
                AutomationJobKind.SEARCH,
                AutomationJobState.RUNNING,
                heartbeat_at=now - timedelta(minutes=3),
                next_run_at=now - timedelta(minutes=4),
            )
        )
        == "STOPPED"
    )


def test_overdue_job_without_active_handler_is_reported_as_stopped() -> None:
    now = datetime.now(UTC)

    assert (
        background_status(
            job(
                "messages:1",
                AutomationJobKind.MESSAGES,
                AutomationJobState.WAITING,
                heartbeat_at=now - timedelta(minutes=5),
                next_run_at=now - timedelta(minutes=3),
            )
        )
        == "STOPPED"
    )
