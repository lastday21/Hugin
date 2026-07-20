from __future__ import annotations

import getpass
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import ClassVar

import pytest

from hugin import hh_cli
from hugin.core.settings import Settings
from hugin.domain.hh import (
    HhApplyResult,
    HhApplyStatus,
    HhProfileData,
    HhResumeData,
    HhResumeDetails,
)
from hugin.domain.vacancies import VacancyData, VacancySearchResult
from hugin.services.hh_login import HhCredentials, LoginStatus
from hugin.services.vacancy_analysis import RuleCategory


class FakeStore:
    def __init__(self, credentials: HhCredentials | None = None) -> None:
        self.credentials = credentials
        self.saved: tuple[int, HhCredentials] | None = None
        self.deleted = False

    def save(self, account_id: int, credentials: HhCredentials) -> None:
        self.saved = (account_id, credentials)

    def load(self, account_id: int) -> HhCredentials | None:
        assert account_id > 0
        return self.credentials

    def delete(self, account_id: int) -> bool:
        assert account_id > 0
        return self.deleted


class FakeBrowser:
    result = LoginStatus.MANUAL_ACTION_REQUIRED
    authenticated = False
    created: FakeBrowser | None = None

    def __init__(
        self,
        profile_dir: Path,
        login_url: str,
        resumes_url: str,
        search_url: str,
        timeout_ms: int,
    ) -> None:
        self.profile_dir = profile_dir
        self.login_url = login_url
        self.resumes_url = resumes_url
        self.search_url = search_url
        self.timeout_ms = timeout_ms
        self.opened = False
        self.details_read: list[str] = []
        self.applications: list[tuple[str, str, str]] = []
        FakeBrowser.created = self

    def __enter__(self) -> FakeBrowser:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def open_login(self) -> None:
        self.opened = True

    def is_authenticated(self) -> bool:
        return self.authenticated

    def submit_credentials(self, credentials: HhCredentials) -> LoginStatus:
        assert credentials.password == "secret"
        return self.result

    def read_profile(self) -> HhProfileData:
        return HhProfileData(
            external_id="12345",
            label="Иван Иванов",
            resumes=(HhResumeData("resume-1", "Python-разработчик"),),
        )

    def search_vacancies(
        self,
        query: str,
        *,
        area: str = "",
        filters: dict[str, object] | None = None,
        page_number: int = 0,
    ) -> VacancySearchResult:
        assert query == "Python backend"
        assert area == "113"
        assert filters == {"order_by": "publication_time"}
        assert page_number == 0
        return VacancySearchResult(
            found=25,
            vacancies=(
                VacancyData(
                    hh_id="vacancy-1",
                    title="Python-разработчик",
                    source_url="https://hh.ru/vacancy/vacancy-1",
                    employer_name="Компания",
                ),
            ),
        )

    def read_vacancy_details(self, source_url: str) -> VacancyData:
        self.details_read.append(source_url)
        return VacancyData(
            hh_id="vacancy-1",
            title="Python-разработчик",
            source_url=source_url,
            description="Python backend",
        )

    def read_resume_details(self, resume_id: str) -> HhResumeDetails:
        return HhResumeDetails(
            hh_id=resume_id,
            title="Python backend разработчик",
            experience="FastAPI PostgreSQL",
            skills="Python Docker",
            education="Высшее",
        )

    def apply_to_vacancy(
        self,
        source_url: str,
        *,
        expected_resume_title: str,
        cover_letter: str,
    ) -> HhApplyResult:
        self.applications.append((source_url, expected_resume_title, cover_letter))
        return HhApplyResult(HhApplyStatus.APPLIED, source_url, "успешно")


class FakeDatabase:
    def __init__(self) -> None:
        self.closed = False

    @property
    def sessions(self) -> FakeDatabase:
        return self

    @contextmanager
    def begin(self) -> Iterator[object]:
        yield object()

    def close(self) -> None:
        self.closed = True


def install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store: FakeStore,
) -> None:
    monkeypatch.setattr(hh_cli, "WindowsCredentialStore", lambda: store)
    monkeypatch.setattr(hh_cli, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(
        hh_cli,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path),
    )


def test_save_reads_password_without_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore()
    monkeypatch.setattr(hh_cli, "WindowsCredentialStore", lambda: store)
    monkeypatch.setattr("builtins.input", lambda prompt: "person@example.com")
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "secret")

    assert hh_cli.run(["save", "--account-id", "4"]) == 0
    assert store.saved == (4, HhCredentials("person@example.com", "secret"))
    assert "защищённом хранилище" in capsys.readouterr().out


@pytest.mark.parametrize(("deleted", "message"), [(True, "удалены"), (False, "не найдено")])
def test_delete_reports_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    deleted: bool,
    message: str,
) -> None:
    store = FakeStore()
    store.deleted = deleted
    monkeypatch.setattr(hh_cli, "WindowsCredentialStore", lambda: store)

    assert hh_cli.run(["delete"]) == 0
    assert message in capsys.readouterr().out


def test_login_reuses_authenticated_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeBrowser.authenticated = True
    install_fakes(monkeypatch, tmp_path, FakeStore())

    assert hh_cli.run(["login", "--account-id", "2"]) == 0
    assert FakeBrowser.created is not None
    assert FakeBrowser.created.opened
    assert FakeBrowser.created.profile_dir == tmp_path / "browser-profiles" / "account-2"


def test_manual_confirmation_can_finish_in_open_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeBrowser.authenticated = False
    FakeBrowser.result = LoginStatus.CONFIRMATION_REQUIRED
    install_fakes(
        monkeypatch,
        tmp_path,
        FakeStore(HhCredentials("person@example.com", "secret")),
    )

    def finish_login(prompt: str) -> str:
        assert "нажмите Enter" in prompt
        assert FakeBrowser.created is not None
        FakeBrowser.created.authenticated = True
        return ""

    monkeypatch.setattr("builtins.input", finish_login)

    assert hh_cli.run(["login"]) == 0


def test_login_without_credentials_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeBrowser.authenticated = False
    install_fakes(monkeypatch, tmp_path, FakeStore())

    assert hh_cli.run(["login"]) == 2


def test_sync_reads_profile_and_saves_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    FakeBrowser.authenticated = True
    install_fakes(monkeypatch, tmp_path, FakeStore())
    database = FakeDatabase()
    synchronized = SimpleNamespace(
        account=SimpleNamespace(id=7, label="Иван Иванов"),
        resumes=(SimpleNamespace(title="Python-разработчик", hh_id="resume-1"),),
    )
    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(
        hh_cli,
        "HhProfileSyncService",
        lambda session: SimpleNamespace(synchronize=lambda profile: synchronized),
    )

    assert hh_cli.run(["sync", "--account-id", "2"]) == 0

    output = capsys.readouterr().out
    assert "Иван Иванов (№ 7)" in output
    assert "Python-разработчик (resume-1)" in output
    assert database.closed


def test_search_loads_vacancies_without_applications(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    FakeBrowser.authenticated = True
    install_fakes(monkeypatch, tmp_path, FakeStore())
    database = FakeDatabase()
    synchronized = SimpleNamespace(
        direction=SimpleNamespace(id=3, name="Python backend"),
        vacancies=(SimpleNamespace(title="Python-разработчик", employer_name="Компания"),),
    )
    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(
        hh_cli,
        "HhProfileSyncService",
        lambda session: SimpleNamespace(synchronize=lambda profile: None),
    )
    monkeypatch.setattr(
        hh_cli,
        "JobSearchSyncService",
        lambda session: SimpleNamespace(synchronize=lambda **kwargs: synchronized),
    )

    assert (
        hh_cli.run(
            [
                "search",
                "--direction",
                "Python backend",
                "--resume",
                "Python-разработчик",
                "--query",
                "Python backend",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Python backend (№ 3)" in output
    assert "По запросу найдено на hh.ru: 25" in output
    assert "Python-разработчик — Компания" in output
    assert database.closed


def test_positive_account_id_parser() -> None:
    assert hh_cli.positive_int("3") == 3
    with pytest.raises(Exception, match="положительным"):
        hh_cli.positive_int("0")

    assert hh_cli.non_negative_int("0") == 0
    with pytest.raises(Exception, match="отрицательным"):
        hh_cli.non_negative_int("-1")


def test_analyze_loads_details_and_prints_rule_reasons(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    FakeBrowser.authenticated = True
    install_fakes(monkeypatch, tmp_path, FakeStore())
    database = FakeDatabase()
    pending = (
        SimpleNamespace(
            title="Python-разработчик",
            source_url="https://hh.ru/vacancy/vacancy-1",
        ),
    )
    evaluation = SimpleNamespace(
        accepted=True,
        category=RuleCategory.MATCH,
        score=75.0,
        reasons=("Python указан в названии",),
    )
    analyzed = (
        SimpleNamespace(
            vacancy=SimpleNamespace(title="Python-разработчик"),
            evaluation=evaluation,
        ),
    )

    class FakeAnalysisService:
        def __init__(self, session: object) -> None:
            assert session is not None

        def pending(self, **kwargs: object) -> tuple[SimpleNamespace, ...]:
            assert kwargs["limit"] == 20
            return pending

        def synchronize(self, **kwargs: object) -> tuple[SimpleNamespace, ...]:
            details = kwargs["vacancies"]
            assert isinstance(details, tuple)
            assert details[0].description == "Python backend"
            return analyzed

        def reanalyze(self, **kwargs: object) -> tuple[SimpleNamespace, ...]:
            assert kwargs["direction_name"] == "Python backend"
            return analyzed

    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(
        hh_cli,
        "HhProfileSyncService",
        lambda session: SimpleNamespace(synchronize=lambda profile: None),
    )
    monkeypatch.setattr(hh_cli, "VacancyAnalysisService", FakeAnalysisService)

    assert hh_cli.run(["analyze", "--direction", "Python backend"]) == 0

    output = capsys.readouterr().out
    assert "Проверено вакансий: 1" in output
    assert "Подходят: 1. Пограничные: 0. Отклонены: 0" in output
    assert "Python указан в названии" in output
    assert FakeBrowser.created is not None
    assert FakeBrowser.created.details_read == ["https://hh.ru/vacancy/vacancy-1"]


def test_apply_runs_queue_and_records_confirmed_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    FakeBrowser.authenticated = True
    install_fakes(monkeypatch, tmp_path, FakeStore())
    database = FakeDatabase()
    job = SimpleNamespace(
        vacancy=SimpleNamespace(
            source_url="https://hh.ru/vacancy/100",
            title="Python developer",
        ),
        resume=SimpleNamespace(title="Python backend разработчик"),
        direction_vacancy=SimpleNamespace(rules_details={"category": "MATCH"}),
    )

    class FakeAutomationService:
        jobs: ClassVar[list[SimpleNamespace]] = [job]

        def __init__(self, session: object) -> None:
            assert session is not None

        def resume_after_authentication(self) -> None:
            return None

        def prepare(self, **kwargs: object) -> SimpleNamespace:
            assert kwargs["include_stretch"] is True
            return SimpleNamespace(
                account_id=1,
                direction_id=3,
                resume=SimpleNamespace(hh_id="resume-1"),
                created=1,
                existing=0,
            )

        def applied_since(self, account_id: int, since: object) -> int:
            assert account_id == 1
            assert since is not None
            return 0

        def claim_next(self, direction_id: int) -> SimpleNamespace | None:
            assert direction_id == 3
            return self.jobs.pop(0) if self.jobs else None

        def record_result(self, queued_job: object, result: HhApplyResult) -> SimpleNamespace:
            assert queued_job is job
            assert result.status is HhApplyStatus.APPLIED
            return SimpleNamespace(sent=True, blocking=False)

    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(
        hh_cli,
        "HhProfileSyncService",
        lambda session: SimpleNamespace(synchronize=lambda profile: None),
    )
    monkeypatch.setattr(hh_cli, "ApplicationAutomationService", FakeAutomationService)
    monkeypatch.setattr(
        hh_cli,
        "CoverLetterBuilder",
        lambda: SimpleNamespace(build=lambda vacancy, resume, category: "Письмо"),
    )

    assert hh_cli.run(["apply", "--direction", "Python backend", "--limit", "1"]) == 0

    output = capsys.readouterr().out
    assert "Новых подтверждённых откликов: 1" in output
    assert "Python developer: отклик подтверждён" in output
    assert FakeBrowser.created is not None
    assert FakeBrowser.created.applications == [
        (
            "https://hh.ru/vacancy/100",
            "Python backend разработчик",
            "Письмо",
        )
    ]


def test_apply_stops_queue_when_browser_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeBrowser.authenticated = True
    install_fakes(monkeypatch, tmp_path, FakeStore())
    database = FakeDatabase()
    job = SimpleNamespace(
        vacancy=SimpleNamespace(
            source_url="https://hh.ru/vacancy/100",
            title="Python developer",
        ),
        resume=SimpleNamespace(title="Python backend разработчик"),
        direction_vacancy=SimpleNamespace(rules_details={"category": "MATCH"}),
    )

    class FakeAutomationService:
        jobs: ClassVar[list[SimpleNamespace]] = [job]

        def __init__(self, session: object) -> None:
            assert session is not None

        def resume_after_authentication(self) -> None:
            return None

        def prepare(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                account_id=1,
                direction_id=3,
                resume=SimpleNamespace(hh_id="resume-1"),
                created=1,
                existing=0,
            )

        def applied_since(self, account_id: int, since: object) -> int:
            return 0

        def claim_next(self, direction_id: int) -> SimpleNamespace | None:
            return self.jobs.pop(0) if self.jobs else None

        def record_result(self, queued_job: object, result: HhApplyResult) -> SimpleNamespace:
            assert result.status is HhApplyStatus.UNKNOWN_RESULT
            assert result.confirmation == "Ошибка выполнения: RuntimeError"
            return SimpleNamespace(sent=False, blocking=True)

    def fail_application(*args: object, **kwargs: object) -> HhApplyResult:
        raise RuntimeError("неопределённый сбой браузера")

    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(
        hh_cli,
        "HhProfileSyncService",
        lambda session: SimpleNamespace(synchronize=lambda profile: None),
    )
    monkeypatch.setattr(hh_cli, "ApplicationAutomationService", FakeAutomationService)
    monkeypatch.setattr(
        hh_cli,
        "CoverLetterBuilder",
        lambda: SimpleNamespace(build=lambda vacancy, resume, category: "Письмо"),
    )
    monkeypatch.setattr(FakeBrowser, "apply_to_vacancy", fail_application)

    assert hh_cli.run(["apply", "--direction", "Python backend", "--limit", "1"]) == 3


def test_main_uses_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hh_cli, "run", lambda: 2)

    with pytest.raises(SystemExit) as error:
        hh_cli.main()

    assert error.value.code == 2
