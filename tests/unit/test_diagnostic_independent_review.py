from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hugin.diagnostics import OperationJournal


@pytest.mark.parametrize(
    "message",
    [
        'Response {"api_key": "review-secret", "reason": "timeout"}',
        "Response {'password': 'review-secret', 'reason': 'timeout'}",
        'password="prefix \\" review-secret" reason=timeout',
        "Connection redis://:review-secret@localhost:6379/0 failed: timeout",
        "Connection postgresql+psycopg://user:review-secret%2Fextra@localhost/db: timeout",
        "Request https://service.example/?client_secret=review-secret&reason=timeout",
        'password="prefix review-secret, suffix" reason="connection timeout"',
    ],
)
def test_error_text_hides_artificial_secret_and_preserves_reason(
    tmp_path: Path, message: str
) -> None:
    journal = OperationJournal(tmp_path)
    run = journal.start("external", "request")
    run.fail(RuntimeError(message))
    text = "\n".join(path.read_text(encoding="utf-8") for path in journal.log_dir.glob("*.jsonl"))
    assert "review-secret" not in text
    records = list(journal.entries(status="failed"))
    assert len(records) == 1
    assert records[0]["details"]["error_type"] == "RuntimeError"
    saved = json.loads(
        (tmp_path / f"evidence/models/{run.run_id}-failure.json").read_text(encoding="utf-8")
    )
    assert "timeout" in saved["payload"]["error_message"]
    assert "timeout" in saved["payload"]["traceback"]
    assert "review-secret" not in json.dumps(saved)


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_unicode_in_error_does_not_split_a_valid_json_record(
    tmp_path: Path, separator: str
) -> None:
    journal = OperationJournal(tmp_path)
    journal.record("worker", "first", status="started")
    journal.record("worker", "error", status="failed", reason=f"Before{separator}After")
    journal.record("worker", "last", status="completed")
    records = list(journal.entries())
    assert [row["event"] for row in records] == ["first", "error", "last"]
    assert records[1]["details"]["reason"] == f"Before{separator}After"


@pytest.mark.parametrize("timestamp", ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"])
def test_damaged_timestamp_does_not_hide_other_records(tmp_path: Path, timestamp: str) -> None:
    now = datetime(2026, 9, 5, 9, tzinfo=UTC)
    journal = OperationJournal(tmp_path, clock=lambda: now)
    journal.record("worker", "first", status="completed")
    path = journal.log_dir / "hugin-2026-09-05.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": timestamp, "event": "damaged"}) + "\n")
    journal.record("worker", "last", status="completed")
    assert [row["event"] for row in journal.entries(since=now)] == ["first", "last"]


def test_numeric_usage_from_model_adapters_remains_available(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    journal.start("codex", "generate").succeed(
        input_tokens=123,
        cached_input_tokens=45,
        output_tokens=67,
        reasoning_tokens=0,
        total_tokens=190,
        token_usage_available=True,
        access_token="review-secret",
    )
    result = next(journal.entries(status="completed"))["details"]
    assert result["input_tokens"] == 123
    assert result["cached_input_tokens"] == 45
    assert result["output_tokens"] == 67
    assert result["reasoning_tokens"] == 0
    assert result["total_tokens"] == 190
    assert result["token_usage_available"] is True
    assert result["access_token"] == "***"


@pytest.mark.parametrize("value", ["review-secret", {"reason": "review-secret"}])
def test_usage_keys_cannot_expose_secret_strings_or_nested_objects(
    tmp_path: Path, value: object
) -> None:
    keys = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "token_usage_available",
    )
    journal = OperationJournal(tmp_path)
    journal.record(
        "model",
        "usage",
        status="completed",
        input_tokens=value,
        cached_input_tokens=value,
        output_tokens=value,
        reasoning_tokens=value,
        total_tokens=value,
        token_usage_available=value,
    )
    result = next(journal.entries())["details"]
    assert all(result[key] == "***" for key in keys)
    assert "review-secret" not in json.dumps(result)


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_numeric_usage_is_not_treated_as_a_counter(tmp_path: Path, value: object) -> None:
    journal = OperationJournal(tmp_path)
    journal.record("model", "usage", status="completed", total_tokens=value)
    assert next(journal.entries())["details"]["total_tokens"] == "***"
