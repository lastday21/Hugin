from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hugin.diagnostics import OperationJournal


def test_journal_records_duration_error_trace_and_redacts_secrets(tmp_path: Path) -> None:
    now = datetime(2026, 7, 28, 8, 30, tzinfo=UTC)
    timers = iter((10.0, 10.125))
    journal = OperationJournal(
        tmp_path,
        clock=lambda: now,
        timer=lambda: next(timers),
    )
    telegram_token = "8261238536:AAFuWV2ZNiYQbc3JDMC555555555555555"
    run = journal.start(
        "telegram",
        "connect",
        bot_token=telegram_token,
        nested={"password": "mail-secret"},
    )

    try:
        raise RuntimeError(
            f"Authorization: Bearer abc.def.ghi; token={telegram_token}; "
            "url=https://t.me/hugin_workbot?start=private-code"
        )
    except RuntimeError as error:
        run.fail(error)

    text = (tmp_path / "logs" / "hugin-2026-07-28.jsonl").read_text(encoding="utf-8")
    assert telegram_token not in text
    assert "mail-secret" not in text
    assert "abc.def.ghi" not in text
    assert "private-code" not in text
    entries = [json.loads(line) for line in text.splitlines()]
    assert entries[0]["status"] == "started"
    assert entries[0]["details"]["bot_token"] == "***"
    assert entries[1]["status"] == "failed"
    assert entries[1]["details"]["duration_ms"] == 125
    assert entries[1]["details"]["error_type"] == "RuntimeError"
    assert "Traceback" in entries[1]["details"]["traceback"]


def test_journal_reader_filters_and_ignores_broken_lines(tmp_path: Path) -> None:
    now = datetime(2026, 7, 28, 8, 30, tzinfo=UTC)
    journal = OperationJournal(tmp_path, clock=lambda: now)
    journal.record("search", "cycle", status="completed", found=12)
    journal.record("notifications", "send", status="failed", channel="telegram")
    path = tmp_path / "logs" / "hugin-2026-07-28.jsonl"
    with path.open("a", encoding="utf-8") as file:
        file.write("{broken\n")

    entries = list(
        journal.entries(
            since=datetime(2026, 7, 28, tzinfo=UTC),
            component="notifications",
            status="failed",
        )
    )

    assert len(entries) == 1
    assert entries[0]["event"] == "send"


def test_journal_prunes_only_expired_journal_files(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "hugin-2026-06-01.jsonl"
    current = log_dir / "hugin-2026-07-20.jsonl"
    unrelated = log_dir / "notes.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    current.write_text("{}\n", encoding="utf-8")
    unrelated.write_text("{}\n", encoding="utf-8")
    journal = OperationJournal(tmp_path)

    removed = journal.prune(
        30,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert removed == 1
    assert not old.exists()
    assert current.exists()
    assert unrelated.exists()
