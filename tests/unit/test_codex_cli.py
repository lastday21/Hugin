from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hugin.adapters.codex_cli import CodexCliClient, CodexCliError, find_codex_cli


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
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert values["env"] is not None
    assert "OPENAI_API_KEY" not in values["env"]  # type: ignore[operator]
    assert "Правила" in str(values["input"])
    assert "Данные вакансии" in str(values["input"])


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
