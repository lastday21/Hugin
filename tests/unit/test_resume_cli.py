from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from hugin import resume_cli
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CandidateProfileModel
from hugin.domain.hh import (
    HhProfileData,
    HhResumeData,
    HhResumeDetails,
    HhResumeExperienceBlock,
)
from hugin.domain.resumes import (
    ProfileFactReview,
    ProfileQuestionCandidate,
    ResumeImportResult,
)
from hugin.repositories import AccountRepository, ResumeRepository
from hugin.services.resume_profile import ResumeImportService
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


class FakeLiveBrowser:
    def __init__(
        self,
        details: HhResumeDetails,
        *,
        authenticated: bool = True,
    ) -> None:
        self.details = details
        self.authenticated = authenticated
        self.opened_login = False
        self.read_resume_ids: list[str] = []

    def __enter__(self) -> FakeLiveBrowser:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def open_login(self) -> None:
        self.opened_login = True

    def is_authenticated(self) -> bool:
        return self.authenticated

    def read_profile(self) -> HhProfileData:
        return HhProfileData(
            external_id="account",
            label="Тимур",
            resumes=(HhResumeData(self.details.hh_id, self.details.title),),
        )

    def read_resume_details(self, resume_id: str) -> HhResumeDetails:
        self.read_resume_ids.append(resume_id)
        return self.details


def live_details() -> HhResumeDetails:
    return HhResumeDetails(
        hh_id="abc123",
        title="Python backend-разработчик",
        city="Санкт-Петербург",
        salary="180 000 ₽ на руки",
        employment="полная занятость",
        work_format="удалённо",
        relocation="Не готов к переезду",
        business_trips="готов к редким командировкам",
        experience="PointPulse\nBackend-разработчик",
        experience_blocks=(
            HhResumeExperienceBlock(
                company="PointPulse",
                position="Backend-разработчик",
                period="Январь 2026 — настоящее время",
                description="Разрабатываю серверную часть.",
                text="PointPulse\nBackend-разработчик\nРазрабатываю серверную часть.",
            ),
        ),
        skills="Python, FastAPI, PostgreSQL",
        education="Высшее образование",
        about="Python backend-разработчик.",
    )


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


def test_live_resume_prints_current_fields_without_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser = FakeLiveBrowser(live_details())
    monkeypatch.setattr(
        resume_cli,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path / "data"),
    )
    monkeypatch.setattr(resume_cli, "VisibleHhBrowser", lambda *_args, **_kwargs: browser)

    assert resume_cli.run(["live", "--resume-id", "abc123"]) == 0

    output = capsys.readouterr().out
    assert "Название: Python backend-разработчик" in output
    assert "Город: Санкт-Петербург" in output
    assert "Зарплата: 180 000 ₽ на руки" in output
    assert "PointPulse — Backend-разработчик" in output
    assert "Обо мне:" in output
    assert browser.opened_login
    assert browser.read_resume_ids == ["abc123"]


def test_live_resume_can_use_database_selection_and_print_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser = FakeLiveBrowser(live_details())
    selected: list[int] = []
    monkeypatch.setattr(
        resume_cli,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path / "data"),
    )
    monkeypatch.setattr(resume_cli, "VisibleHhBrowser", lambda *_args, **_kwargs: browser)

    def select_active(_settings: Settings, account_id: int) -> str:
        selected.append(account_id)
        return "abc123"

    monkeypatch.setattr(resume_cli, "_active_hh_resume_id", select_active)

    assert resume_cli.run(["live", "--account-id", "2", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["hh_id"] == "abc123"
    assert payload["work_format"] == "удалённо"
    assert payload["experience_blocks"][0]["company"] == "PointPulse"
    assert selected == [2]


def test_live_resume_requires_existing_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser = FakeLiveBrowser(live_details(), authenticated=False)
    monkeypatch.setattr(
        resume_cli,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path / "data"),
    )
    monkeypatch.setattr(resume_cli, "VisibleHhBrowser", lambda *_args, **_kwargs: browser)

    assert resume_cli.run(["live", "--resume-id", "abc123"]) == 2
    assert "сначала запустите hugin-hh login" in capsys.readouterr().err
    assert browser.read_resume_ids == []


def test_live_resume_rejects_id_from_another_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser = FakeLiveBrowser(live_details())
    monkeypatch.setattr(
        resume_cli,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path / "data"),
    )
    monkeypatch.setattr(resume_cli, "VisibleHhBrowser", lambda *_args, **_kwargs: browser)

    assert resume_cli.run(["live", "--resume-id", "other-id"]) == 2
    assert "отсутствует в текущем аккаунте" in capsys.readouterr().err
    assert browser.read_resume_ids == []


def test_live_resume_reports_missing_browser_components(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        resume_cli,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path / "data"),
    )
    monkeypatch.setattr(resume_cli, "VisibleHhBrowser", None)

    assert resume_cli.run(["live", "--resume-id", "abc123"]) == 2
    assert "требует браузерные компоненты" in capsys.readouterr().err


def test_active_hh_resume_id_reads_selected_resume(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тимур", "resume-live-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "live-resume-id",
                "Python backend-разработчик",
            )
            session.add(
                CandidateProfileModel(
                    account_id=account.id,
                    active_resume_id=resume.id,
                    display_name="Тимур",
                )
            )

        assert resume_cli._active_hh_resume_id(settings, account.id) == "live-resume-id"
    finally:
        database.close()


def test_active_hh_resume_id_rejects_local_import(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тимур", "resume-local-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                ResumeImportService.LOCAL_RESUME_ID,
                "Импортированное резюме",
            )
            session.add(
                CandidateProfileModel(
                    account_id=account.id,
                    active_resume_id=resume.id,
                    display_name="Тимур",
                )
            )

        with pytest.raises(LookupError, match=r"Активное резюме hh\.ru не выбрано"):
            resume_cli._active_hh_resume_id(settings, account.id)
    finally:
        database.close()


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

        def confirm(
            self,
            account_id: int,
            fact_id: int,
            *,
            allow_in_letters: bool,
            allow_in_forms: bool,
            allow_in_messages: bool,
        ) -> None:
            assert allow_in_letters
            assert allow_in_forms
            assert allow_in_messages
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
