from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import hugin.workers.automation as worker_module
from hugin.core.settings import Settings
from hugin.domain import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
    AutomationJobStateError,
)
from hugin.domain.tasks import SystemState
from hugin.workers.automation import (
    AutomationJobBlocked,
    AutomationJobDeferred,
    AutomationJobRetry,
    AutomationWorker,
)


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
        self.ensured_jobs: tuple[AutomationJobRecord, ...] = ()
        self.configured: list[tuple[int, datetime | None]] = []
        self.recovered: list[datetime | None] = []
        self.unblocked: list[tuple[str, datetime | None]] = []
        self.heartbeats: list[tuple[str, datetime | None]] = []
        self.heartbeat_seen = threading.Event()
        self.blocked: list[tuple[str, str, str, datetime | None]] = []
        self.failed: list[tuple[str, str, str, datetime | None]] = []
        self.retry_delays: list[int | None] = []
        self.completed: list[tuple[str, AutomationJobResult, datetime | None]] = []
        self.deferred: list[tuple[str, int, AutomationJobResult | None, datetime | None]] = []

    def ensure_configured_jobs(
        self,
        account_id: int,
        now: datetime | None = None,
    ) -> tuple[AutomationJobRecord, ...]:
        self.configured.append((account_id, now))
        return self.ensured_jobs

    def list_for_account(self, account_id: int) -> tuple[AutomationJobRecord, ...]:
        assert account_id == 1
        return (self.job,) if self.job is not None else ()

    def recover_stale(self, now: datetime | None = None) -> None:
        self.recovered.append(now)

    def unblock(self, job_key: str, now: datetime | None = None) -> None:
        self.unblocked.append((job_key, now))

    def claim_due(self, now: datetime | None = None) -> AutomationJobRecord | None:
        del now
        job = self.job
        self.job = None
        return job

    def heartbeat(self, job_key: str, now: datetime | None = None) -> None:
        self.heartbeats.append((job_key, now))
        self.heartbeat_seen.set()

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
        retry_after_seconds: int | None = None,
        now: datetime | None = None,
    ) -> None:
        self.failed.append((job_key, error_code, error_message, now))
        self.retry_delays.append(retry_after_seconds)

    def complete(
        self,
        job_key: str,
        result: AutomationJobResult,
        now: datetime | None = None,
    ) -> None:
        self.completed.append((job_key, result, now))

    def defer(
        self,
        job_key: str,
        *,
        retry_after_seconds: int,
        result: AutomationJobResult | None = None,
        now: datetime | None = None,
    ) -> None:
        self.deferred.append((job_key, retry_after_seconds, result, now))


def patch_worker_storage(
    monkeypatch: pytest.MonkeyPatch,
    scheduler: FakeScheduler,
    *,
    system_state: SystemState = SystemState.RUNNING,
) -> FakeDatabase:
    database = FakeDatabase()

    def create_database(_settings: Settings) -> FakeDatabase:
        return database

    def create_scheduler(_session: object) -> FakeScheduler:
        return scheduler

    class FakeSystemStateRepository:
        def __init__(self, _session: object) -> None:
            pass

        def get(self) -> SimpleNamespace:
            return SimpleNamespace(state=system_state)

    monkeypatch.setattr(worker_module, "create_database", create_database)
    monkeypatch.setattr(worker_module, "AutomationSchedulerService", create_scheduler)
    monkeypatch.setattr(
        worker_module,
        "SystemStateRepository",
        FakeSystemStateRepository,
    )
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
    tmp_path: Path,
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
    settings = Settings(environment="test", data_dir=tmp_path)
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


def test_worker_stop_timeout_marks_running_job_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = make_job(AutomationJobKind.SEARCH)
    scheduler = FakeScheduler(job)
    database = patch_worker_storage(monkeypatch, scheduler)

    class StubbornThread:
        def is_alive(self) -> bool:
            return True

        def join(self, _timeout: float | None = None) -> None:
            return

    worker = AutomationWorker(Settings(environment="test", data_dir=tmp_path))
    worker._thread = StubbornThread()  # type: ignore[assignment]

    worker.stop(0.01)

    assert worker.running
    assert scheduler.failed == [
        (
            job.key,
            "AUTOMATION_INTERRUPTED",
            "Фоновое задание прервано при закрытии программы",
            None,
        )
    ]
    assert scheduler.retry_delays == [60]
    assert database.closed


def test_connected_handler_unblocks_previous_missing_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    scheduler = FakeScheduler(None)
    scheduler.ensured_jobs = (
        replace(
            make_job(AutomationJobKind.MESSAGES),
            state=AutomationJobState.BLOCKED,
            last_error_code="SOURCE_NOT_CONNECTED",
        ),
    )
    patch_worker_storage(monkeypatch, scheduler)
    worker = AutomationWorker(
        Settings(environment="test"),
        handlers={AutomationJobKind.MESSAGES: lambda _job: {}},
    )

    assert not worker.run_once(now)
    assert scheduler.unblocked == [("messages:1", now)]


@pytest.mark.parametrize(
    (
        "account_id",
        "poll_seconds",
        "heartbeat_seconds",
        "authentication_recovery_interval_seconds",
    ),
    [
        (0, 2.0, 30.0, 60.0),
        (1, 0.0, 30.0, 60.0),
        (1, 2.0, 0.0, 60.0),
        (1, 2.0, 30.0, 0.0),
    ],
)
def test_worker_rejects_invalid_settings(
    account_id: int,
    poll_seconds: float,
    heartbeat_seconds: float,
    authentication_recovery_interval_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        AutomationWorker(
            Settings(environment="test"),
            account_id=account_id,
            poll_seconds=poll_seconds,
            heartbeat_seconds=heartbeat_seconds,
            authentication_recovery_interval_seconds=(authentication_recovery_interval_seconds),
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


@pytest.mark.parametrize(
    "system_state",
    [SystemState.AUTH_REQUIRED, SystemState.CAPTCHA_REQUIRED],
)
def test_worker_retries_authentication_on_a_bounded_interval(
    monkeypatch: pytest.MonkeyPatch,
    system_state: SystemState,
) -> None:
    now = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
    scheduler = FakeScheduler(None)
    patch_worker_storage(monkeypatch, scheduler, system_state=system_state)
    attempts: list[SystemState] = []

    def recover() -> bool:
        attempts.append(system_state)
        return False

    worker = AutomationWorker(
        Settings(environment="test"),
        authentication_recovery=recover,
        authentication_recovery_interval_seconds=60,
    )

    assert worker.run_once(now)
    assert not worker.run_once(now.replace(second=30))
    assert worker.run_once(now.replace(minute=1))

    assert attempts == [system_state, system_state]


def test_worker_does_not_recover_account_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
    scheduler = FakeScheduler(None)
    patch_worker_storage(
        monkeypatch,
        scheduler,
        system_state=SystemState.ACCOUNT_WARNING,
    )
    attempts = 0

    def recover() -> bool:
        nonlocal attempts
        attempts += 1
        return True

    worker = AutomationWorker(
        Settings(environment="test"),
        authentication_recovery=recover,
    )

    assert not worker.run_once(now)
    assert attempts == 0


def test_worker_blocks_job_when_handler_reports_required_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job()
    scheduler = FakeScheduler(job)
    database = patch_worker_storage(monkeypatch, scheduler)

    def blocked(_job: AutomationJobRecord) -> AutomationJobResult:
        raise AutomationJobBlocked("  CAPTCHA_REQUIRED  ", "Пройдите проверку")

    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
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
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job()
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    def fail(_job: AutomationJobRecord) -> AutomationJobResult:
        raise SilentFailure

    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
        handlers={AutomationJobKind.MESSAGES: fail},
    )

    assert worker.run_once(now)
    assert len(scheduler.failed) == 1
    assert scheduler.failed[0][0] == job.key
    assert scheduler.failed[0][1] == "SilentFailure"
    assert scheduler.failed[0][2]


def test_worker_schedules_explicit_retry_delay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job()
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    def retry(_job: AutomationJobRecord) -> AutomationJobResult:
        raise AutomationJobRetry(
            "HH_RATE_LIMITED",
            "hh.ru временно ограничил обращения",
            retry_after_seconds=180,
        )

    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
        handlers={AutomationJobKind.MESSAGES: retry},
    )

    assert worker.run_once(now)
    assert scheduler.failed == [
        (job.key, "HH_RATE_LIMITED", "hh.ru временно ограничил обращения", now)
    ]
    assert scheduler.retry_delays == [180]
    assert not scheduler.blocked
    assert scheduler.failed[0][3] == now
    assert not scheduler.blocked
    assert not scheduler.completed


def test_worker_defers_search_without_recording_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job(AutomationJobKind.SEARCH)
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    def defer(_job: AutomationJobRecord) -> AutomationJobResult:
        raise AutomationJobDeferred(
            "APPLICATIONS_PENDING",
            "Сначала обрабатываются найденные вакансии",
            retry_after_seconds=60,
        )

    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
        handlers={AutomationJobKind.SEARCH: defer},
    )

    assert worker.run_once(now)
    assert scheduler.deferred == [
        (
            job.key,
            60,
            {"deferred": True, "reason": "APPLICATIONS_PENDING"},
            now,
        )
    ]
    assert not scheduler.failed
    assert not scheduler.blocked


def test_worker_keeps_previous_result_when_job_is_deferred(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = replace(
        make_job(AutomationJobKind.MESSAGES),
        last_result={"message_baseline_initialized": True},
    )
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    def defer(_job: AutomationJobRecord) -> AutomationJobResult:
        raise AutomationJobDeferred(
            "APPLICATION_READY",
            "Сначала отправляется готовый отклик",
            retry_after_seconds=60,
        )

    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
        handlers={AutomationJobKind.MESSAGES: defer},
    )

    assert worker.run_once(now)
    assert scheduler.deferred == [
        (
            job.key,
            60,
            {
                "message_baseline_initialized": True,
                "deferred": True,
                "reason": "APPLICATION_READY",
            },
            now,
        )
    ]
    assert not scheduler.failed
    assert not scheduler.blocked


def test_worker_completes_successful_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job()
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    def complete(_job: AutomationJobRecord) -> AutomationJobResult:
        return {"checked": 3}

    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
        handlers={AutomationJobKind.MESSAGES: complete},
    )

    assert worker.run_once(now)
    assert scheduler.completed == [(job.key, {"checked": 3}, now)]
    assert not scheduler.blocked
    assert not scheduler.failed


def test_worker_updates_heartbeat_from_a_separate_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job(AutomationJobKind.SEARCH)
    scheduler = FakeScheduler(job)
    databases: list[FakeDatabase] = []

    def create_database(_settings: Settings) -> FakeDatabase:
        database = FakeDatabase()
        databases.append(database)
        return database

    def create_scheduler(_session: object) -> FakeScheduler:
        return scheduler

    monkeypatch.setattr(worker_module, "create_database", create_database)
    monkeypatch.setattr(worker_module, "AutomationSchedulerService", create_scheduler)
    monkeypatch.setattr(
        worker_module,
        "SystemStateRepository",
        lambda _session: SimpleNamespace(get=lambda: SimpleNamespace(state=SystemState.RUNNING)),
    )

    def complete(_job: AutomationJobRecord) -> AutomationJobResult:
        assert scheduler.heartbeat_seen.wait(1.0)
        return {"checked": 3}

    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
        handlers={AutomationJobKind.SEARCH: complete},
        heartbeat_seconds=0.01,
    )

    assert worker.run_once(now)
    assert scheduler.heartbeats
    assert all(heartbeat == (job.key, None) for heartbeat in scheduler.heartbeats)
    assert scheduler.completed == [(job.key, {"checked": 3}, now)]
    assert len(databases) == 2
    assert all(database.closed for database in databases)


def test_heartbeat_records_database_creation_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_database(_settings: Settings) -> FakeDatabase:
        raise RuntimeError("База временно недоступна")

    monkeypatch.setattr(worker_module, "create_database", fail_database)
    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
        heartbeat_seconds=0.001,
    )

    worker._heartbeat_job("search:7", threading.Event())

    entries = tuple(worker._journal.entries(component="automation", status="failed"))
    assert entries[-1]["event"] == "job.heartbeat"
    assert entries[-1]["details"]["job_key"] == "search:7"


def test_heartbeat_stops_when_job_is_no_longer_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = FakeDatabase()

    class StoppedScheduler(FakeScheduler):
        def heartbeat(self, job_key: str, now: datetime | None = None) -> None:
            del now
            raise AutomationJobStateError(
                job_key,
                AutomationJobState.WAITING,
                AutomationJobState.RUNNING,
            )

    scheduler = StoppedScheduler(None)
    monkeypatch.setattr(worker_module, "create_database", lambda _settings: database)
    monkeypatch.setattr(
        worker_module,
        "AutomationSchedulerService",
        lambda _session: scheduler,
    )
    worker = AutomationWorker(
        Settings(environment="test", data_dir=tmp_path),
        heartbeat_seconds=0.001,
    )

    worker._heartbeat_job("search:7", threading.Event())

    assert database.closed


def test_worker_blocks_job_without_connected_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    job = make_job(AutomationJobKind.SEARCH)
    scheduler = FakeScheduler(job)
    patch_worker_storage(monkeypatch, scheduler)

    assert AutomationWorker(Settings(environment="test", data_dir=tmp_path)).run_once(now)
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
