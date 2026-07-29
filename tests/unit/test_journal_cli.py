# ruff: noqa: RUF001

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hugin import journal_cli
from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal


def test_show_summary_and_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(environment="test", data_dir=tmp_path)
    journal = OperationJournal(
        tmp_path,
        clock=lambda: datetime.now(UTC),
    )
    journal.record("search", "cycle", status="completed", found=5)
    journal.record(
        "notifications",
        "send",
        status="failed",
        error_type="RuntimeError",
        error_message="Нет подключения",
    )
    monkeypatch.setattr(journal_cli, "get_settings", lambda: settings)

    assert journal_cli.main(["show", "--hours", "1", "--limit", "10"]) == 0
    shown = capsys.readouterr().out
    assert "notifications | send | failed" in shown
    assert "RuntimeError: Нет подключения" in shown

    assert journal_cli.main(["summary", "--hours", "1"]) == 0
    summary = capsys.readouterr().out
    assert "Всего записей: 2" in summary
    assert "Ошибки и блокировки: 1" in summary

    target = tmp_path / "exports" / "journal.jsonl"
    assert journal_cli.main(["export", str(target), "--hours", "1"]) == 0
    capsys.readouterr()
    exported = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(exported) == 2
