from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hugin.diagnostics import OperationJournal, operation_context


def test_nested_operation_context_cannot_escape_into_another_operation(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    with operation_context(application_id=12):
        with pytest.raises(RuntimeError), operation_context(application_id=13):
            journal.start("worker", "operation").fail(RuntimeError("stopped"))
            raise RuntimeError("stopped")
        journal.record("worker", "parent", status="completed")
    journal.record("worker", "unrelated", status="completed")
    rows = list(journal.entries())
    assert rows[1]["details"]["application_id"] == 13
    assert rows[2]["details"]["application_id"] == 12
    assert "application_id" not in rows[3].get("details", {})


def test_oversized_and_nested_diagnostic_values_stay_bounded_and_readable(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    cycle: list[Any] = []
    cycle.append(cycle)
    journal.record(
        "worker",
        "bounded",
        status="failed",
        account_id=7,
        mapping={str(index): index for index in range(200)},
        values=(value for value in range(200)),
        cycle=cycle,
        error_message="failure " * 10000,
        happened=date(2026, 9, 5),
        path=Path("secret=artificial-private"),
        opaque=object(),
    )
    path = next(journal.log_dir.glob("*.jsonl"))
    raw = path.read_text(encoding="utf-8")
    assert len(raw) < 15000 and "artificial-private" not in raw
    row = json.loads(raw)
    assert row["details"]["account_id"] == 7
    assert len(list(journal.entries())) == 1


def test_storage_failure_is_reported_without_stopping_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = OperationJournal(tmp_path)
    (tmp_path / "logs").write_text("not a directory", encoding="utf-8")
    assert journal.record("worker", "write", status="failed") is False
    assert list(journal.entries()) == []
    assert journal.prune() == 0
    with pytest.raises(ValueError):
        OperationJournal(tmp_path, retention_days=-1)
    with pytest.raises(ValueError):
        journal.prune(-1)
    with pytest.raises(ValueError):
        journal.record(" ", "write", status="completed")


def test_retention_removes_only_old_owned_evidence_and_preserves_fresh_sources(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    journal = OperationJournal(tmp_path, retention_days=3, clock=lambda: now)
    assert journal.save_evidence("old", "request", {"source": "old"})
    assert journal.save_evidence("new", "request", {"source": "new"})
    directory = tmp_path / "evidence/models"
    old = directory / "old-request.json"
    stale = (now - timedelta(days=10)).timestamp()
    os.utime(old, (stale, stale))
    unrelated = directory / "personal-note.json"
    unrelated.write_text("keep", encoding="utf-8")
    os.utime(unrelated, (stale, stale))
    assert journal.record("worker", "cleanup", status="completed")
    assert not old.exists()
    assert (directory / "new-request.json").exists() and unrelated.exists()
    assert journal.record("worker", "second", status="completed")


def test_reader_survives_a_disappearing_file_and_invalid_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    journal = OperationJournal(tmp_path, clock=lambda: now)
    journal.record("worker", "valid", status="completed")
    directory = journal.log_dir
    (directory / "hugin-nonsense.jsonl").write_text("{}\n", encoding="utf-8")
    (directory / "hugin-2000-01-01.jsonl").write_text(
        '{}\n{"timestamp":12}\n{"timestamp":"bad"}\n', encoding="utf-8"
    )
    original = Path.read_text

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name == "hugin-2000-01-01.jsonl":
            raise OSError("disappeared")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert [row["event"] for row in journal.entries(since=now)] == ["valid"]
    monkeypatch.setattr(Path, "read_text", original)
    assert [row["event"] for row in journal.entries(since=now)] == ["valid"]
