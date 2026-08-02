from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from sqlalchemy.orm import Session

from hugin.domain import AutomationJobKind, AutomationJobState
from hugin.services.ui_workspace import UiWorkspaceService


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
