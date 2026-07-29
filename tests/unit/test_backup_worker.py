from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import ApplicationSettingsModel, IncidentModel
from hugin.domain.content import IncidentState
from hugin.workers import backups as worker_module


def test_worker_uses_saved_retention_and_resolves_incident(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            application_settings = session.get(ApplicationSettingsModel, 1)
            assert application_settings is not None
            application_settings.backups_retention_days = 14
    finally:
        database.close()
    values: list[int] = []

    class Service:
        def __init__(self, selected: Settings) -> None:
            assert selected is settings

        def ensure_daily(self, *, retention_days: int) -> object:
            values.append(retention_days)
            return object()

    monkeypatch.setattr("hugin.workers.backups.BackupService", Service)
    worker = worker_module.BackupWorker(settings)
    assert worker.run_once()
    worker._record_failure("Ошибка резервной копии")
    worker._record_failure("Новая ошибка резервной копии")
    worker._resolve_failure()

    database = create_database(settings)
    try:
        with database.sessions() as session:
            incident = session.query(IncidentModel).filter_by(code="BACKUP_FAILED").one()
            assert incident.state is IncidentState.RESOLVED
    finally:
        database.close()
    assert values == [14]


def test_worker_start_stop_and_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        worker_module.BackupWorker(Settings(environment="test"), poll_seconds=0)

    class Thread:
        def __init__(self, **values: object) -> None:
            assert values["name"] == "hugin-backups"
            self.alive = False
            self.joined: list[float] = []

        def start(self) -> None:
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float) -> None:
            self.joined.append(timeout)
            self.alive = False

    threads: list[Thread] = []

    def thread(**values: object) -> Thread:
        created = Thread(**values)
        threads.append(created)
        return created

    monkeypatch.setattr(threading, "Thread", thread)
    worker = worker_module.BackupWorker(Settings(environment="test", data_dir=tmp_path))
    worker.start()
    worker.start()
    assert worker.running
    worker.stop(0.5)
    worker.stop()
    assert not worker.running
    assert threads[0].joined == [0.5]


def test_worker_loop_reports_only_new_error_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []

    class Stop:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, seconds: float) -> None:
            events.append(("wait", seconds))
            self.stopped = True

    class Toast:
        def send(self, content: object) -> None:
            events.append(("toast", content))

    worker = worker_module.BackupWorker(
        Settings(environment="test", data_dir=tmp_path),
        poll_seconds=2,
    )
    monkeypatch.setattr(worker, "_log_retention_days", lambda: 90)
    worker._stop = Stop()  # type: ignore[assignment]
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda: (_ for _ in ()).throw(RuntimeError("failure")),
    )
    monkeypatch.setattr(
        worker, "_record_failure", lambda message: events.append(("failed", message))
    )
    monkeypatch.setattr(worker_module, "WindowsToastSender", Toast)

    worker._run()

    assert [event[0] for event in events if isinstance(event, tuple)] == [
        "failed",
        "toast",
        "wait",
    ]

    events.clear()
    worker._stop = Stop()  # type: ignore[assignment]
    worker._run()
    assert [event[0] for event in events if isinstance(event, tuple)] == [
        "failed",
        "wait",
    ]


def test_worker_loop_resolves_failure_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []

    class Stop:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, seconds: float) -> None:
            events.append(("wait", seconds))
            self.stopped = True

    worker = worker_module.BackupWorker(
        Settings(environment="test", data_dir=tmp_path),
        poll_seconds=3,
    )
    monkeypatch.setattr(worker, "_log_retention_days", lambda: 90)
    worker._stop = Stop()  # type: ignore[assignment]
    worker._last_reported_error = "old"
    monkeypatch.setattr(worker, "run_once", lambda: False)
    monkeypatch.setattr(worker, "_resolve_failure", lambda: events.append("resolved"))

    worker._run()

    assert worker._last_reported_error is None
    assert events == ["resolved", ("wait", 3)]
