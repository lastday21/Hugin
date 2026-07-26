from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
)
from hugin.repositories import AccountRepository
from hugin.services import AutomationSchedulerService
from hugin.workers.automation import AutomationWorker

pytestmark = pytest.mark.integration


def seed_account(settings: Settings, external_id: str) -> int:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            return AccountRepository(session).create("Фоновая проверка", external_id).id
    finally:
        database.close()


def test_worker_runs_due_jobs_once_without_catch_up(settings: Settings) -> None:
    account_id = seed_account(settings, "worker-account")
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    calls: list[AutomationJobKind] = []

    def record(job: AutomationJobRecord) -> AutomationJobResult:
        calls.append(job.kind)
        return {"checked": 0}

    worker = AutomationWorker(
        settings,
        account_id=account_id,
        handlers={
            AutomationJobKind.MESSAGES: record,
            AutomationJobKind.STATUSES: record,
        },
    )

    assert worker.run_once(now)
    assert worker.run_once(now)
    assert not worker.run_once(now)
    assert calls == [AutomationJobKind.MESSAGES, AutomationJobKind.STATUSES]

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = AutomationSchedulerService(session).list_for_account(account_id)
            messages = next(job for job in jobs if job.kind is AutomationJobKind.MESSAGES)
            statuses = next(job for job in jobs if job.kind is AutomationJobKind.STATUSES)
            assert messages.state is AutomationJobState.WAITING
            assert messages.next_run_at == now + timedelta(minutes=5)
            assert statuses.state is AutomationJobState.WAITING
            assert statuses.next_run_at == now + timedelta(minutes=30)
    finally:
        database.close()


def test_worker_blocks_missing_sources_instead_of_retrying(settings: Settings) -> None:
    account_id = seed_account(settings, "worker-blocked")
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    worker = AutomationWorker(settings, account_id=account_id)

    assert worker.run_once(now)
    assert worker.run_once(now)
    assert not worker.run_once(now)

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = AutomationSchedulerService(session).list_for_account(account_id)
            assert {job.state for job in jobs} == {AutomationJobState.BLOCKED}
            assert {job.last_error_code for job in jobs} == {"SOURCE_NOT_CONNECTED"}
    finally:
        database.close()
