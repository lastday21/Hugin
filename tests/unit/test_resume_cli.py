from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from hugin import resume_cli
from hugin.core.settings import Settings
from hugin.domain.resumes import (
    ProfileFactReview,
    ProfileQuestionCandidate,
    ResumeImportResult,
)
from tests.unit.test_resume_documents import write_resume


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


def configure_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> FakeDatabase:
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    database = FakeDatabase()
    monkeypatch.setattr(resume_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(resume_cli, "upgrade_database", lambda _: None)
    monkeypatch.setattr(resume_cli, "create_database", lambda _: database)
    return database


def test_inspect_resume_without_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "resume.docx"
    write_resume(path)

    assert resume_cli.run(["inspect", str(path)]) == 0
    output = capsys.readouterr().out
    assert "Формат: DOCX" in output
    assert "Должность: Python backend разработчик" in output
    assert "salary_expectation" in output


def test_inspect_rejects_missing_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert resume_cli.run(["inspect", str(tmp_path / "missing.pdf")]) == 2
    error = capsys.readouterr().err
    assert "Ошибка:" in error


def test_import_command_reports_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = configure_database(monkeypatch, tmp_path)
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"pdf")
    imported = ResumeImportResult(
        resume_id=7,
        title="Python backend разработчик",
        stored_path=tmp_path / "stored.pdf",
        source_sha256="a" * 64,
        facts_pending=17,
        questions_pending=(ProfileQuestionCandidate("salary", "Зарплата?"),),
        unchanged=False,
    )

    class FakeImportService:
        def __init__(self, _session: object, _data_dir: Path) -> None:
            pass

        def import_file(
            self,
            account_id: int,
            file: Path,
            *,
            hh_resume_id: str | None,
        ) -> ResumeImportResult:
            assert account_id == 1
            assert file == source
            assert hh_resume_id == "hh-id"
            return imported

    monkeypatch.setattr(resume_cli, "ResumeImportService", FakeImportService)

    assert (
        resume_cli.run(["import", str(source), "--account-id", "1", "--hh-resume-id", "hh-id"]) == 0
    )
    output = capsys.readouterr().out
    assert "резюме № 7" in output
    assert "Фактов ждут подтверждения: 17" in output
    assert "Исходный файл сохранён" in output
    assert database.closed


def test_fact_review_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = configure_database(monkeypatch, tmp_path)
    actions: list[tuple[str, int]] = []

    class FakeFactService:
        def __init__(self, _session: object) -> None:
            pass

        def list_pending(self, account_id: int) -> tuple[ProfileFactReview, ...]:
            assert account_id == 1
            return (ProfileFactReview(3, "skills", "Python " * 40),)

        def confirm(self, account_id: int, fact_id: int) -> None:
            actions.append(("confirm", fact_id))

        def reject(self, account_id: int, fact_id: int) -> None:
            actions.append(("reject", fact_id))

    monkeypatch.setattr(resume_cli, "ProfileFactService", FakeFactService)

    assert resume_cli.run(["facts"]) == 0
    assert "3 [skills]" in capsys.readouterr().out
    assert resume_cli.run(["confirm-fact", "--fact-id", "3"]) == 0
    assert "Факт подтверждён" in capsys.readouterr().out
    assert resume_cli.run(["reject-fact", "--fact-id", "3"]) == 0
    assert "Факт отклонён" in capsys.readouterr().out
    assert actions == [("confirm", 3), ("reject", 3)]
    assert database.closed


def test_question_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_database(monkeypatch, tmp_path)
    answers: list[tuple[str, str]] = []

    class FakeQuestionService:
        def __init__(self, _session: object) -> None:
            pass

        def list_pending(self, account_id: int) -> tuple[ProfileQuestionCandidate, ...]:
            return (ProfileQuestionCandidate("salary", "Какая зарплата?"),)

        def answer(self, account_id: int, key: str, answer: str) -> None:
            answers.append((key, answer))

    monkeypatch.setattr(resume_cli, "ProfileQuestionService", FakeQuestionService)
    monkeypatch.setattr("builtins.input", lambda _: "180 000 рублей")

    assert resume_cli.run(["questions"]) == 0
    assert "salary: Какая зарплата?" in capsys.readouterr().out
    assert resume_cli.run(["answer", "--key", "salary"]) == 0
    assert "Ответ сохранён" in capsys.readouterr().out
    assert answers == [("salary", "180 000 рублей")]


def test_database_command_error_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_database(monkeypatch, tmp_path)

    class FailingQuestionService:
        def __init__(self, _session: object) -> None:
            pass

        def list_pending(self, _account_id: int) -> tuple[ProfileQuestionCandidate, ...]:
            raise LookupError("нет профиля")

    monkeypatch.setattr(resume_cli, "ProfileQuestionService", FailingQuestionService)

    assert resume_cli.run(["questions"]) == 2
    assert "нет профиля" in capsys.readouterr().err
