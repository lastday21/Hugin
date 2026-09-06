import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal
from hugin.services.operation_timing import history_quality, task_timing, timing_report
from hugin.workers.applications import ApplicationWorker

START = datetime(2026, 9, 6, tzinfo=UTC)


def stage(run: str, start: int, end: int, **details: object) -> list[dict[str, Any]]:
    return [
        {
            "component": "applications",
            "event": "step",
            "run_id": run,
            "timestamp": (START + timedelta(seconds=second)).isoformat(),
            "status": status,
            "details": details,
        }
        for second, status in ((start, "started"), (end, "completed"))
    ]


def test_nested_work_and_wait_are_not_counted_twice() -> None:
    rows = stage("outer", 10, 60) + stage("inner", 20, 40)
    rows += stage("wait", 10, 25, timing_kind="wait", reason="BROWSER_BUSY")
    report = timing_report(rows, since=START, until=START + timedelta(seconds=70))
    assert report["elapsed_ms"] == 70000
    assert report["work_ms"] == 35000
    assert report["wait_ms"] == 15000
    assert report["unclassified_ms"] == 20000


def test_open_application_lifetime_is_not_execution_time() -> None:
    rows = stage("session", 0, 60)
    for row in rows:
        row.update(component="desktop", event="application.session")
    rows += stage("actual_work", 20, 30)
    report = timing_report(rows, since=START, until=START + timedelta(seconds=60))
    assert report["work_ms"] == 10000
    assert report["unclassified_ms"] == 50000


def test_instant_notification_is_not_an_unfinished_stage() -> None:
    row = stage("notification", 0, 10)[-1]
    report = timing_report([row], since=START, until=START + timedelta(seconds=10))
    assert report["incomplete_runs"] == []


def test_wait_in_another_worker_does_not_hide_work() -> None:
    rows = stage("work", 0, 30)
    for row in rows:
        row.update(process_id=1, thread="sync")
    waiting = stage("wait", 0, 30, timing_kind="wait")
    for row in waiting:
        row.update(process_id=1, thread="applications")
    report = timing_report(rows + waiting, since=START, until=START + timedelta(seconds=30))
    assert report["work_ms"] == 30000
    assert report["wait_ms"] == 0
    assert report["observed_wait_ms"] == 30000


def test_open_stage_is_unknown_and_window_clips_completed_stages() -> None:
    rows = stage("completed", -5, 5) + stage("unfinished", 5, 10)[:1]
    report = timing_report(rows, since=START, until=START + timedelta(seconds=10))
    assert report["work_ms"] == 5000
    assert report["unclassified_ms"] == 5000
    assert report["incomplete_runs"] == ["unfinished"]
    with pytest.raises(ValueError):
        timing_report(rows, since=START, until=START - timedelta(seconds=1))


def test_stage_started_before_window_remains_visible_as_unfinished() -> None:
    rows = stage("unfinished_chat", 0, 30)[:1]
    report = timing_report(
        rows, since=START + timedelta(seconds=5), until=START + timedelta(seconds=15)
    )
    assert report["incomplete_runs"] == ["unfinished_chat"]
    assert report["work_ms"] == 0
    assert report["unclassified_ms"] == 10000


def test_missing_whole_step_is_detected_and_other_parent_cannot_replace_it() -> None:
    rows = stage("parent", 0, 20, required_steps=["execute", "persist"])
    rows += stage("execute", 0, 10, parent_run_id="parent", step_name="execute")
    rows += stage("wrong", 10, 20, parent_run_id="another", step_name="persist")
    report = history_quality(rows)
    assert report["complete"] is False
    assert report["issues"][0]["missing_steps"] == ["persist"]


def test_failed_step_is_present_but_unfinished_step_is_not_complete() -> None:
    rows = stage("parent", 0, 20, required_steps=["execute"])
    child = stage("execute", 0, 10, parent_run_id="parent", step_name="execute")
    child[-1]["status"] = "failed"
    assert history_quality(rows + child)["complete"] is True
    assert history_quality(rows + child[:1])["complete"] is False
    assert history_quality([])["complete"] is None


def test_step_records_failure_and_inherits_context(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    run = journal.start("applications", "apply", application_id=42)
    with pytest.raises(ValueError), run.step("execute"):
        raise ValueError("failure")
    rows = journal.entries()
    children = [row for row in rows if row["details"].get("step_name") == "execute"]
    assert [row["status"] for row in children] == ["started", "failed"]
    assert all(row["details"]["parent_run_id"] == run.run_id for row in children)
    assert all(row["details"]["application_id"] == 42 for row in children)


def test_browser_wait_is_one_interval_not_one_record_per_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = threading.Lock()
    journal = OperationJournal(tmp_path)
    worker = ApplicationWorker(Settings(data_dir=tmp_path), browser_lock=lock, journal=journal)
    monkeypatch.setattr(worker, "has_pending_work", lambda *args: True)
    monkeypatch.setattr(worker, "_claim", lambda *args: (None, False))
    lock.acquire()
    assert worker.run_once() is False
    assert worker.run_once() is False
    lock.release()
    assert worker.run_once() is False
    rows = [row for row in journal.entries() if row["event"] == "browser.wait"]
    assert [row["status"] for row in rows] == ["started", "completed"]
    assert rows[0]["details"]["timing_kind"] == "wait"


def test_paused_queue_does_not_report_browser_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = threading.Lock()
    journal = OperationJournal(tmp_path)
    worker = ApplicationWorker(Settings(data_dir=tmp_path), browser_lock=lock, journal=journal)
    monkeypatch.setattr(worker, "has_pending_work", lambda *_: False)
    with lock:
        assert worker.run_once() is False
    assert not list(journal.entries())


def test_task_total_includes_time_before_execution_without_inventing_reason() -> None:
    task = {
        "id": 1,
        "state": "COMPLETED",
        "created_at": START.isoformat(),
        "updated_at": (START + timedelta(seconds=600)).isoformat(),
    }
    rows = stage("apply", 420, 600, task_id=1)
    report = task_timing(task, rows, now=START + timedelta(hours=1))
    assert report["elapsed_ms"] == 600000
    assert report["work_ms"] == 180000
    assert report["unclassified_ms"] == 420000
    assert report["wait_ms"] == 0


def test_task_with_invalid_dates_does_not_break_diagnostics() -> None:
    report = task_timing(
        {"id": 1, "state": "COMPLETED", "created_at": "bad", "updated_at": START.isoformat()},
        [],
        now=START,
    )
    assert report["elapsed_ms"] is None
    assert report["issue"] == "invalid_task_dates"


def test_timeline_endpoint_validates_dates(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from hugin.api.app import create_app

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/diagnostics/timeline", params={"since": START.isoformat()})
        assert response.status_code == 200
        assert "unclassified_ms" in response.json()["timing"]
        assert (
            client.get("/api/diagnostics/timeline", params={"since": "2026-09-06"}).status_code
            == 422
        )


def test_server_journal_does_not_export_messages_and_can_be_combined(settings: Settings) -> None:
    from hugin.database import create_database
    from hugin.services.operation_trace import OperationTraceService

    now = datetime.now(UTC)
    journal = OperationJournal(settings.data_dir)
    run = journal.start("queue", "resume", private_note="personal text")
    run.succeed()
    database = create_database(settings)
    try:
        with database.sessions() as session:
            service = OperationTraceService(session, data_dir=settings.data_dir)
            exported = service.journal_window(since=now, until=datetime.now(UTC))
            assert "personal text" not in str(exported)
            assert "private_note" not in str(exported)
            first = service.timeline(since=now, until=now + timedelta(seconds=2))
            combined = service.timeline(
                since=now, until=now + timedelta(seconds=2), additional_records=exported["records"]
            )
            assert combined["timing"] == first["timing"]
    finally:
        database.close()


def test_partial_time_window_keeps_whole_history_family(settings: Settings) -> None:
    from hugin.database import create_database
    from hugin.services.operation_trace import OperationTraceService

    ticks = iter([0, 0, 10, 10, 20, 20])
    journal = OperationJournal(
        settings.data_dir, clock=lambda: START + timedelta(seconds=next(ticks))
    )
    run = journal.start("automation", "scheduled_job", required_steps=["execute", "persist"])
    with run.step("execute"):
        pass
    with run.step("persist"):
        pass
    run.succeed()
    database = create_database(settings)
    try:
        with database.sessions() as session:
            service = OperationTraceService(session, data_dir=settings.data_dir)
            since, until = START + timedelta(seconds=15), START + timedelta(seconds=25)
            report = service.timeline(since=since, until=until)
            assert report["history"]["complete"] is True
            exported = service.journal_window(since=since, until=until)
            assert history_quality(exported["records"])["complete"] is True
            assert report["timing"]["work_ms"] == 5000
    finally:
        database.close()


@pytest.mark.parametrize("source", ["server", "unavailable", "local"])
def test_timeline_command_records_source_availability(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    from hugin import diagnostic_cli

    monkeypatch.setattr(diagnostic_cli, "get_settings", lambda: settings)

    def get(_client: httpx.Client, url: str, **_kwargs: Any) -> httpx.Response:
        assert source != "local"
        if source == "unavailable":
            raise httpx.ConnectError("unavailable")
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"records": stage("server", 0, 2), "journal_read_issues": []},
        )

    monkeypatch.setattr(httpx.Client, "get", get)
    path = tmp_path / "timeline.json"
    args = [
        "timeline",
        "--since",
        START.isoformat(),
        "--until",
        (START + timedelta(seconds=3)).isoformat(),
        "--output",
        str(path),
    ]
    if source == "local":
        args.append("--local-only")
    assert diagnostic_cli.main(args) == 0
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["server_journal_checked"] is (source == "server")
    if source == "server":
        assert report["timing"]["work_ms"] == 2000
    elif source == "unavailable":
        assert report["server_journal_issues"][0]["reason"] == "server_journal_unavailable"


def test_timeline_reports_background_jobs_and_required_steps(settings: Settings) -> None:
    from hugin.database import create_database
    from hugin.services.operation_trace import OperationTraceService

    journal = OperationJournal(settings.data_dir)
    run = journal.start(
        "automation", "scheduled_job", job_kind="MESSAGES", required_steps=["execute"]
    )
    with run.step("execute"):
        pass
    run.succeed()
    database = create_database(settings)
    try:
        with database.sessions() as session:
            report = OperationTraceService(session, data_dir=settings.data_dir).timeline(
                since=START, until=datetime.now(UTC) + timedelta(days=1)
            )
        assert report["history"]["complete"] is True
        assert any(item["job_kind"] == "MESSAGES" for item in report["timing"]["intervals"])
    finally:
        database.close()
