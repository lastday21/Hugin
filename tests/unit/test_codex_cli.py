from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hugin.adapters.codex_cli import CodexCliClient, CodexCliError, find_codex_cli


class RecordingRun:
    def __init__(self) -> None:
        self.completed: dict[str, object] = {}

    def succeed(self, **details: object) -> None:
        self.completed = details

    def fail(self, _error: Exception, **_details: object) -> None:
        return None


class RecordingJournal:
    def __init__(self) -> None:
        self.run = RecordingRun()
        self.started: dict[str, object] = {}

    def start(self, _component: str, _operation: str, **details: object) -> RecordingRun:
        self.started = details
        return self.run


def make_client(tmp_path: Path) -> CodexCliClient:
    executable = tmp_path / "codex.cmd"
    executable.touch()
    return CodexCliClient(executable, tmp_path / "runtime")


def test_client_uses_subscription_login_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    def run(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="Здравствуйте!\n\nГотовое письмо.\n", stderr="")

    monkeypatch.setattr("hugin.adapters.codex_cli.subprocess.run", run)

    result = make_client(tmp_path).complete("Правила", "Данные вакансии")

    assert result == "Здравствуйте!\n\nГотовое письмо."
    command, values = calls[0]
    assert command[1:4] == ["exec", "--ephemeral", "--ignore-user-config"]
    assert "--ignore-rules" in command
    assert "--json" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert values["env"] is not None
    assert "OPENAI_API_KEY" not in values["env"]  # type: ignore[operator]
    assert "Правила" in str(values["input"])
    assert "Данные вакансии" in str(values["input"])


def test_client_reads_text_and_token_usage_from_json_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stdout = "\n".join(
        (
            '{"type":"thread.started","thread_id":"thread-1"}',
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"Здравствуйте!\\n\\nГотовое письмо."}}',
            '{"type":"turn.completed","usage":{"input_tokens":1200,'
            '"cached_input_tokens":800,"output_tokens":150,"reasoning_tokens":40}}',
        )
    )
    monkeypatch.setattr(
        "hugin.adapters.codex_cli.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )
    executable = tmp_path / "codex.cmd"
    executable.touch()
    journal = RecordingJournal()
    client = CodexCliClient(executable, tmp_path / "runtime", journal=journal)  # type: ignore[arg-type]

    result = client.complete("Правила", "Данные вакансии")

    assert result == "Здравствуйте!\n\nГотовое письмо."
    assert journal.run.completed == {
        "output_characters": len(result),
        "token_usage_available": True,
        "input_tokens": 1200,
        "cached_input_tokens": 800,
        "output_tokens": 150,
        "reasoning_tokens": 40,
        "total_tokens": 1350,
    }
    assert journal.started["system_prompt_characters"] == 7
    assert journal.started["user_prompt_characters"] == 15
    request_characters = journal.started["request_characters"]
    assert isinstance(request_characters, int)
    assert request_characters > 22


@pytest.mark.parametrize(
    ("return_code", "stdout", "stderr", "message"),
    [
        (1, "", "подписка временно недоступна", "подписка временно недоступна"),
        (0, "", "", "пустой ответ"),
    ],
)
def test_client_reports_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    return_code: int,
    stdout: str,
    stderr: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        "hugin.adapters.codex_cli.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=return_code,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    with pytest.raises(CodexCliError, match=message):
        make_client(tmp_path).complete("Правила", "Задание")


def test_client_reports_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def timeout(command: list[str], **kwargs: object) -> object:
        del kwargs
        raise subprocess.TimeoutExpired(command, 180)

    monkeypatch.setattr("hugin.adapters.codex_cli.subprocess.run", timeout)

    with pytest.raises(CodexCliError, match="Истекло время"):
        make_client(tmp_path).complete("Правила", "Задание")


def test_configured_path_must_exist(tmp_path: Path) -> None:
    missing = tmp_path / "missing-codex.cmd"

    with pytest.raises(LookupError, match="не найдена"):
        find_codex_cli(missing)


def test_recruiter_reply_prompt_requests_an_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def run(_command: list[str], **kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="Готов обсудить вопрос.", stderr="")

    monkeypatch.setattr("hugin.adapters.codex_cli.subprocess.run", run)
    executable = tmp_path / "codex.cmd"
    executable.touch()
    client = CodexCliClient(
        executable,
        tmp_path / "runtime",
        operation="recruiter_reply",
    )

    client.complete("Правила", "Переписка")

    prompt = str(calls[0]["input"])
    assert "итоговый ответ работодателю" in prompt
    assert "итоговое сопроводительное письмо" not in prompt


def test_reply_requirement_prompt_requests_only_a_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def run(_command: list[str], **kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="NO_REPLY_REQUIRED", stderr="")

    monkeypatch.setattr("hugin.adapters.codex_cli.subprocess.run", run)
    executable = tmp_path / "codex.cmd"
    executable.touch()
    client = CodexCliClient(
        executable,
        tmp_path / "runtime",
        model="gpt-5.6-luna",
        operation="recruiter_reply_requirement",
    )

    client.complete("Правила", "Переписка")

    prompt = str(calls[0]["input"])
    assert "решение REPLY_REQUIRED или NO_REPLY_REQUIRED" in prompt
    assert "итоговое сопроводительное письмо" not in prompt
