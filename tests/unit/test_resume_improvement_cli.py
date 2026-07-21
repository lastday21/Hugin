from __future__ import annotations

import getpass
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from hugin import resume_improvement_cli
from hugin.adapters.yandex_credentials import YandexAICredentials
from hugin.core.settings import Settings
from hugin.services.resume_improvement import (
    AnswerProvider,
    ImprovedResumeBlock,
    ResumeImprovementResult,
    ResumeNarrativeBlock,
)
from hugin.services.resume_prompts import ResumeBlockKind


class FakeStore:
    def __init__(self, credentials: YandexAICredentials | None = None) -> None:
        self.credentials = credentials
        self.saved: YandexAICredentials | None = None
        self.deleted = False

    def save(self, credentials: YandexAICredentials) -> None:
        self.saved = credentials

    def load(self) -> YandexAICredentials | None:
        return self.credentials

    def delete(self) -> bool:
        self.deleted = True
        return self.credentials is not None


class FakeClient:
    model_name = "fake-model"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def complete(self, _system_prompt: str, _user_prompt: str) -> str:
        return "готово"


class FakeSessions:
    @contextmanager
    def begin(self) -> Iterator[object]:
        yield object()


class FakeDatabase:
    def __init__(self) -> None:
        self.sessions = FakeSessions()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_configure_saves_key_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore()
    monkeypatch.setattr(
        resume_improvement_cli,
        "WindowsYandexAICredentialStore",
        lambda: store,
    )
    monkeypatch.setattr(resume_improvement_cli, "get_settings", lambda: Settings())
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "secret-key")

    assert (
        resume_improvement_cli.run(
            ["configure", "--folder-id", "folder", "--model", "yandexgpt/latest"]
        )
        == 0
    )
    assert store.saved == YandexAICredentials("secret-key", "folder", "yandexgpt/latest")
    assert "secret-key" not in capsys.readouterr().out


def test_connection_command_uses_saved_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore(YandexAICredentials("key", "folder"))
    monkeypatch.setattr(
        resume_improvement_cli,
        "WindowsYandexAICredentialStore",
        lambda: store,
    )
    monkeypatch.setattr(resume_improvement_cli, "YandexAIClient", FakeClient)
    monkeypatch.setattr(resume_improvement_cli, "get_settings", lambda: Settings())

    assert resume_improvement_cli.run(["test"]) == 0
    assert "fake-model: готово" in capsys.readouterr().out


def test_run_uses_separate_service_and_reports_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore(YandexAICredentials("key", "folder"))
    database = FakeDatabase()
    block = ResumeNarrativeBlock(
        index=1,
        kind=ResumeBlockKind.PROJECT,
        label="Проект отчетов",
        start_line=5,
        end_line=8,
        source_text="Исходный текст",
    )
    improved = ImprovedResumeBlock(
        index=1,
        kind=block.kind,
        label=block.label,
        source_text=block.source_text,
        improved_text="Новый текст",
        questions=("Какой результат?",),
        answers=("Сократил ручную работу.",),
    )
    result = ResumeImprovementResult(
        resume_id=2,
        target_role="Python-разработчик",
        model_name="fake-model",
        draft_path=tmp_path / "draft.docx",
        report_path=tmp_path / "report.json",
        blocks=(improved,),
        source_unchanged=True,
    )
    received_answers: list[str] = []

    class FakeService:
        def __init__(self, _session: object, _data_dir: Path, _client: object) -> None:
            pass

        def improve(
            self,
            account_id: int,
            answer_provider: AnswerProvider,
            *,
            target_role: str | None,
            vacancy_limit: int,
        ) -> ResumeImprovementResult:
            assert account_id == 1
            assert target_role == "Python-разработчик"
            assert vacancy_limit == 25
            answer = answer_provider(block, "Какой результат?")
            received_answers.append(answer)
            return result

    monkeypatch.setattr(
        resume_improvement_cli,
        "WindowsYandexAICredentialStore",
        lambda: store,
    )
    monkeypatch.setattr(resume_improvement_cli, "YandexAIClient", FakeClient)
    monkeypatch.setattr(
        resume_improvement_cli,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path / "data"),
    )
    monkeypatch.setattr(resume_improvement_cli, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(resume_improvement_cli, "create_database", lambda _settings: database)
    monkeypatch.setattr(resume_improvement_cli, "ResumeImprovementService", FakeService)
    monkeypatch.setattr("builtins.input", lambda _prompt: "Сократил ручную работу.")

    assert (
        resume_improvement_cli.run(
            [
                "run",
                "--account-id",
                "1",
                "--target-role",
                "Python-разработчик",
                "--vacancy-limit",
                "25",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "проект «Проект отчетов»" in output
    assert str(result.draft_path) in output
    assert "На hh.ru ничего не опубликовано" in output
    assert received_answers == ["Сократил ручную работу."]
    assert database.closed
