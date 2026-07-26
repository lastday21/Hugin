from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

import hugin.workers.automation as worker_module
from hugin.core.settings import Settings
from hugin.domain import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
)
from hugin.workers.automation import AutomationJobBlocked, AutomationWorker


def make_job(kind: AutomationJobKind = AutomationJobKind.MESSAGES) -> AutomationJobRecord:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    search_query_id = 7 if kind is AutomationJobKind.SEARCH else None
    return AutomationJobRecord(
        key=f"{kind.value.lower()}:1",
        kind=kind,
        state=AutomationJobState.RUNNING,
        account_id=1,
        search_query_id=search_query_id,
        interval_seconds=300,
        next_run_at=now,
        last_started_at=now,
        last_finished_at=None,
        last_success_at=None,
        heartbeat_at=now,
        consecutive_failures=0,
        last_error_code=None,
        last_error_message=None,
        last_result={},
        created_at=now,
        updated_at=now,
    )


class FakeSessions:
    @contextmanager
    def begin(self) -> Iterator[object]:
        yield object()


class FakeDatabase:
    def __init__(self) -> None:
        self.sessions = FakeSessions()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeScheduler:
    def __init__(self, job: AutomationJobRecord | None) -> None:
        self.job = job
        self.configured: list[tuple[int, datetime | None]] = []
        self.recovered: list[datetime | None] = []
        self.blocked: list[tuple[str, str, str, datetime | None]] = []
        self.failed: list[tuple[str, str, str, datetime | None]] = []
        self.completed: list[tuple[str, AutomationJobResult, datetime | None]] = []

    def ensure_configured_jobs(
        self,
        account_id: int,
        now: datetime | None = None,
    ) -> None:
        self.configured.append((account_id, now))

    def recover_stale(self, now: datetime | None = None) -> None:
        self.recovered.append(now)

    def claim_due(self, now: datetime | None = None) -> AutomationJobRecord | None:
        del now
        job = self.job
        self.job = None
        return job

    def block(
        self,
        job_key: str,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> None:
        self.blocked.append((job_key, error_code, error_message, now))

    def fail(
        self,
        job_key: str,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> None:
        self.failed.append((job_key, error_code, error_message, now))

    def complete(
        self,
        job_key: str,
        result: AutomationJobResult,
        now: datetime | None = None,
    ) -> None:
        self.completed.append((job_key, result, now))


def patch_worker_storage(
    monkeypatch: pytest.MonkeyPatch,
    scheduler: FakeScheduler,
) -> FakeDatabase:
    database = FakeDatabase()

    def create_database(_settings: Settings) -> FakeDatabase:
        return database

    def create_scheduler(_session: object) -> FakeScheduler:
        return scheduler

    monkeypatch.setattr(worker_module, "create_database", create_database)
    monkeypatch.setattr(worker_module, "AutomationSchedulerService", create_scheduler)
    return database


class FakeThread:
    def __init__(
        self,
        target: Callable[[], None],
        name: str,
        daemon: bool,
    ) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.alive = False
        self.started = 0
        self.joined_with: list[float | None] = []

    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        self.started += 1
        self.alive = True

    def join(self, timeout: float | None = None) -> None:
        self.joined_with.append(timeout)
        self.alive = False


def test_worker_start_stop_and_restart_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrades: list[Settings] = []
    threads: list[FakeThread] = []

    def upgrade_database(settings: Settings) -> None:
        upgrades.append(settings)

    def create_thread(
        *,
        target: Callable[[], None],
        name: str,
        daemon: bool,
    ) -> FakeThread:
        thread = FakeThread(target, name, daemon)
        threads.append(thread)
        return thread

    monkeypatch.setattr(worker_module, "upgrade_database", upgrade_database)
    monkeypatch.setattr("hugin.workers.automation.threading.Thread", create_thread)
    settings = Settings(environment="test")
    worker = AutomationWorker(settings)

    assert not worker.running
    worker.start()
    worker.start()

    assert worker.running
    assert len(upgrades) == 1
    assert len(threads) == 1
    assert threads[0].started == 1
    assert threads[0].name == "hugin-background-checks"
    assert threads[0].daemon

    worker.stop(0.25)
    worker.stop()

    assert not worker.running
    assert threads[0].joined_with == [0.25]

    worker.start()

    assert worker.running
    assert len(upgrades) == 2
    assert len(threads) == 2
    worker.stop()


@pytest.mark.parametrize(("account_id", "poll_seconds"), [(0, 2.0), (1, 0.0)])
def test_worker_rejects_invalid_settings(account_id: int, poll_seconds: float) -> None:
    with pytest.raises(ValueError):
        AutomationWorker(
            Settings(environment="test"),
            account_id=account_id,
            poll_seconds=poll_seconds,
        )


def test_worker_returns_false_when_no_job_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    scheduler = FakeScheduler(None)
    database = patch_worker_storage(monkeypatch, scheduler)

    assert not AutomationWorker(Settings(environment="test")).run_once(now)
    assert scheduler.configured == [(1, now)]
    assert scheduler.recovered == [now]
    assert database.closed


def test_worker_blocks_job_when_handler_reports_required_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job()
    scheduler = FakeScheduler(job)
    database = patch_worker_storage(monkeypatch, scheduler)

    def blocked(_job: AutomationJobRecord) -> AutomationJobResult:
        raise AutomationJobBlocked("  CAPTCHA_REQUIRED  ", "Пройдите проверку")

    worker = AutomationWorker(
        Settings(environment="test"),
        handlers={AutomationJobKind.MESSAGES: blocked},
    )

    assert worker.run_once(now)
    assert scheduler.blocked == [(job.key, "CAPTCHA_REQUIRED", "Пройдите проверку", now)]
    assert not scheduler.failed
    assert not scheduler.completed
    assert database.closed


class SilentFailure(RuntimeError):
    def __str__(self) -> str:
        return ""


def test_worker_records_unexpected_handler_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job()
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    def fail(_job: AutomationJobRecord) -> AutomationJobResult:
        raise SilentFailure

    worker = AutomationWorker(
        Settings(environment="test"),
        handlers={AutomationJobKind.MESSAGES: fail},
    )

    assert worker.run_once(now)
    assert len(scheduler.failed) == 1
    assert scheduler.failed[0][0] == job.key
    assert scheduler.failed[0][1] == "SilentFailure"
    assert scheduler.failed[0][2]
    assert scheduler.failed[0][3] == now
    assert not scheduler.blocked
    assert not scheduler.completed


def test_worker_completes_successful_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job()
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    def complete(_job: AutomationJobRecord) -> AutomationJobResult:
        return {"checked": 3}

    worker = AutomationWorker(
        Settings(environment="test"),
        handlers={AutomationJobKind.MESSAGES: complete},
    )

    assert worker.run_once(now)
    assert scheduler.completed == [(job.key, {"checked": 3}, now)]
    assert not scheduler.blocked
    assert not scheduler.failed


def test_worker_blocks_job_without_connected_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job(AutomationJobKind.SEARCH)
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    assert AutomationWorker(Settings(environment="test")).run_once(now)
    assert len(scheduler.blocked) == 1
    assert scheduler.blocked[0][0] == job.key
    assert scheduler.blocked[0][1] == "SOURCE_NOT_CONNECTED"
    assert "hh.ru" in scheduler.blocked[0][2]


@pytest.mark.parametrize("worked", [False, True])
def test_worker_loop_stops_after_current_iteration(
    monkeypatch: pytest.MonkeyPatch,
    worked: bool,
) -> None:
    worker = AutomationWorker(Settings(environment="test"), poll_seconds=0.01)
    calls = 0

    def run_once(_now: datetime | None = None) -> bool:
        nonlocal calls
        calls += 1
        worker._stop.set()
        return worked

    monkeypatch.setattr(worker, "run_once", run_once)

    worker._run()

    assert calls == 1
