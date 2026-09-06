from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from hugin.adapters.codex_cli import CodexCliClient, CodexCliError
from hugin.adapters.yandex_ai import YandexAIClient, YandexAIError
from hugin.diagnostics import OperationJournal
from tests.unit.test_yandex_ai import FakeResponse


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "empty", "launch"])
def test_codex_failure_keeps_raw_response_out_of_public_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    executable = tmp_path / "codex.exe"
    executable.touch()
    raw = '{"type":"turn.failed","detail":"private-diagnostic-876"}'

    def run(*args: object, **kwargs: object) -> object:
        if failure == "timeout":
            raise subprocess.TimeoutExpired("codex", 1, output=raw.encode(), stderr=b"partial")
        if failure == "launch":
            raise OSError("unavailable")
        return SimpleNamespace(returncode=1 if failure == "nonzero" else 0, stdout=raw, stderr="")

    monkeypatch.setattr("hugin.adapters.codex_cli.subprocess.run", run)
    journal = OperationJournal(tmp_path)
    with pytest.raises(CodexCliError):
        CodexCliClient(executable, tmp_path / "runtime", journal=journal).complete("rule", "input")
    files = list((tmp_path / "evidence/models").glob("*-response.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))["payload"]
    if failure != "launch":
        assert saved["stdout"] == raw
    assert next(journal.entries(status="failed"))["details"]["response_evidence_saved"] is True


@pytest.mark.parametrize("failure", ["timeout", "http", "empty", "url", "socket"])
def test_yandex_failure_keeps_received_stream_for_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    raw = b'data: {"choices":[{"delta":{"content":"private-partial-876"}}]}\n'

    class InterruptedResponse(FakeResponse):
        def __iter__(self) -> Iterator[bytes]:
            yield raw
            raise TimeoutError("stream interrupted")

    def open_request(*args: object, **kwargs: object) -> FakeResponse:
        if failure == "http":
            raise urllib.error.HTTPError("http://test", 503, "down", Message(), BytesIO(raw))
        if failure == "url":
            raise urllib.error.URLError("unavailable")
        if failure == "socket":
            raise OSError("socket reset")
        if failure == "empty":
            return FakeResponse([b"data: []\n", b"data: [DONE]\n"])
        return InterruptedResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    journal = OperationJournal(tmp_path)
    with pytest.raises(YandexAIError):
        YandexAIClient("key", "folder", journal=journal).complete("rules", "input")
    files = list((tmp_path / "evidence/models").glob("*-response.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))["payload"]
    if failure in {"timeout", "http"}:
        assert "private-partial-876" in json.dumps(saved)
    assert next(journal.entries(status="failed"))["details"]["response_evidence_saved"] is True
