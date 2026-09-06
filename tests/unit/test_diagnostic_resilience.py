from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hugin.diagnostics import OperationJournal


@pytest.mark.parametrize(
    "message",
    [
        "Could not connect to postgresql+psycopg://candidate:private-value@localhost:5432/hugin",
        "Request failed https://service.example/auth?access_token=private-value&reason=timeout",
        "Request failed https://service.example/auth?api_key=private-value&reason=timeout",
        "Request failed https://service.example/auth?refresh_token=private-value&reason=timeout",
        "Credential rejected password='first private-value' reason=timeout",
        'Credential rejected secret="first private-value" reason=timeout',
    ],
)
def test_failed_operation_keeps_diagnostic_reason_without_credentials(
    tmp_path: Path, message: str
) -> None:
    journal = OperationJournal(tmp_path)
    run = journal.start("external", "request")
    run.fail(RuntimeError(message))
    entries = list(journal.entries(status="failed"))
    assert len(entries) == 1
    assert entries[0]["details"]["error_type"] == "RuntimeError"
    serialized = json.dumps(entries)
    assert "private-value" not in serialized
    saved = json.loads(
        next((tmp_path / "evidence/models").glob("*-failure.json")).read_text(encoding="utf-8")
    )
    assert "***" in json.dumps(saved)
    assert "private-value" not in json.dumps(saved)
    if "reason=timeout" in message:
        assert "reason=timeout" in saved["payload"]["error_message"]


def test_invalid_date_in_log_name_does_not_stop_recording_or_delete_file(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    invalid = logs / "hugin-2026-99-99.jsonl"
    invalid.write_text("diagnostic evidence", encoding="utf-8")
    journal = OperationJournal(tmp_path)
    assert journal.record("applications", "recovery", status="completed", application_id=7)
    assert invalid.read_text(encoding="utf-8") == "diagnostic evidence"
    assert any(row.get("event") == "recovery" for row in journal.entries())


def test_invalid_bytes_do_not_hide_valid_records_in_the_same_log(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    journal = OperationJournal(tmp_path, clock=lambda: now)
    assert journal.record("applications", "recovery", status="started")
    path = tmp_path / "logs" / "hugin-2026-09-05.jsonl"
    with path.open("ab") as stream:
        stream.write(b"\xff\xfe broken record\n")
    assert journal.record("applications", "recovery", status="completed")
    assert [row["status"] for row in journal.entries()] == ["started", "completed"]
