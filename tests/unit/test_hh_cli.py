from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import ClassVar, cast

import pytest

from hugin import hh_cli
from hugin.core.settings import Settings
from hugin.domain.directions import EmploymentForm, SearchRegion, VacancyState, WorkFormat
from hugin.domain.hh import (
    HhApplyResult,
    HhApplyStatus,
    HhProfileData,
    HhResumeData,
    HhResumeDetails,
)
from hugin.domain.resumes import ProfileQuestionCandidate
from hugin.domain.tasks import SystemState, TaskState
from hugin.domain.vacancies import VacancyAvailability, VacancyData, VacancySearchResult
from hugin.services.career_directions import DirectionSearchSettings, VacancySearchTask
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
        vacancies=(
            SimpleNamespace(
                id=1,
                title="Python-разработчик",
                employer_name="Компания",
            ),
        ),
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
    assert "Выполнено вариантов поиска: 1" in output
    assert "найдено 25, загружено 1" in output
    assert "Уникальных вакансий в базе: 1" in output
    assert "Python-разработчик — Компания" in output
    assert database.closed


def test_positive_account_id_parser() -> None:
    assert hh_cli.positive_int("3") == 3
    with pytest.raises(Exception, match="положительным"):
        hh_cli.positive_int("0")

    assert hh_cli.non_negative_int("0") == 0
    with pytest.raises(Exception, match="отрицательным"):
        hh_cli.non_negative_int("-1")


def test_search_setting_value_parsers() -> None:
    assert hh_cli.search_region("  Москва ") == SearchRegion("1", "Москва")
    assert hh_cli.search_region("Иннополис=1652") == SearchRegion("1652", "Иннополис")
    with pytest.raises(Exception, match="неизвестный город"):
        hh_cli.search_region("Иннополис")
    with pytest.raises(Exception, match="формате"):
        hh_cli.search_region("Иннополис=нет")

    assert hh_cli.work_format("удаленно") is WorkFormat.REMOTE
    assert hh_cli.work_format("офис") is WorkFormat.ON_SITE
    assert hh_cli.work_format("гибрид") is WorkFormat.HYBRID
    with pytest.raises(Exception, match="формат"):
        hh_cli.work_format("поле")

    assert hh_cli.employment_form("полная") is EmploymentForm.FULL
    assert hh_cli.employment_form("частичная") is EmploymentForm.PART
    assert hh_cli.employment_form("проект") is EmploymentForm.PROJECT
    assert hh_cli.employment_form("вахта") is EmploymentForm.FLY_IN_FLY_OUT
    with pytest.raises(Exception, match="занятость"):
        hh_cli.employment_form("любая")


def test_cities_command_prints_known_cities(capsys: pytest.CaptureFixture[str]) -> None:
    assert hh_cli.run(["cities"]) == 0
    output = capsys.readouterr().out
    assert "Москва: 1" in output
    assert "Екатеринбург: 3" in output


def test_main_allows_unrepresentable_vacancy_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeOutput:
        def reconfigure(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", FakeOutput())
    monkeypatch.setattr(hh_cli, "run", lambda: 0)

    with pytest.raises(SystemExit) as error:
        hh_cli.main()

    assert error.value.code == 0
    assert calls == [{"errors": "replace"}]


def test_display_text_normalizes_typographic_dashes_and_spaces() -> None:
    assert hh_cli._display_text("Python\u2011developer\u00a0API") == "Python-developer API"


def test_configure_and_show_search_settings_without_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = FakeDatabase()
    configured = SimpleNamespace(name="configured")
    calls: list[tuple[str, object]] = []

    class FakeDirectionService:
        def __init__(self, session: object) -> None:
            calls.append(("session", session))

        def configure(self, **kwargs: object) -> object:
            calls.append(("configure", kwargs))
            return configured

        def get(self, account_id: int, direction_name: str) -> object:
            calls.append(("get", (account_id, direction_name)))
            return configured

    class FakeProfileQuestionService:
        def __init__(self, session: object) -> None:
            pass

        def list_pending(self, account_id: int) -> tuple[ProfileQuestionCandidate, ...]:
            assert account_id == 1
            return (ProfileQuestionCandidate("salary", "Какая зарплата?"),)

    install_fakes(monkeypatch, tmp_path, FakeStore())
    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(hh_cli, "CareerDirectionService", FakeDirectionService)
    monkeypatch.setattr(hh_cli, "ProfileQuestionService", FakeProfileQuestionService)
    monkeypatch.setattr(
        hh_cli,
        "_print_search_settings",
        lambda value: calls.append(("print", value)),
    )
    monkeypatch.setattr(
        hh_cli,
        "_print_pending_questions",
        lambda value: calls.append(("questions", value)),
    )

    assert (
        hh_cli.run(
            [
                "configure-search",
                "--direction",
                "ИТ",
                "--query",
                "Python",
                "--city",
                "Москва",
                "--format",
                "удалённо",
                "--employment",
                "полная",
                "--minimum-salary",
                "180000",
                "--desired-salary",
                "220000",
                "--remote-russia",
            ]
        )
        == 0
    )
    assert hh_cli.run(["show-search", "--direction", "ИТ"]) == 0
    configure_call = next(value for name, value in calls if name == "configure")
    assert isinstance(configure_call, dict)
    assert configure_call["regions"] == (SearchRegion("1", "Москва"),)
    assert configure_call["work_formats"] == (WorkFormat.REMOTE,)
    assert configure_call["employment_forms"] == (EmploymentForm.FULL,)
    assert ("get", (1, "ИТ")) in calls
    assert any(name == "questions" for name, _ in calls)
    assert database.closed


def test_queue_settings_pause_and_status_work_without_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = FakeDatabase()
    calls: list[tuple[str, object]] = []
    policy = SimpleNamespace(
        daily_limit=30,
        delay_min_seconds=35,
        delay_max_seconds=55,
        timezone_name="UTC+05:00",
    )
    system = SimpleNamespace(state=SystemState.PAUSED, next_apply_at=None)
    status = SimpleNamespace(
        policy=policy,
        system=system,
        task_counts={TaskState.PENDING: 4},
    )

    class FakeQueueService:
        def __init__(self, session: object) -> None:
            assert session is not None

        def configure(self, **kwargs: object) -> SimpleNamespace:
            calls.append(("configure", kwargs))
            return policy

        def pause(self) -> SimpleNamespace:
            calls.append(("pause", None))
            return system

        def policy(self, timezone_name: str) -> SimpleNamespace:
            calls.append(("policy", timezone_name))
            return policy

        def status(self) -> SimpleNamespace:
            return status

    install_fakes(monkeypatch, tmp_path, FakeStore())
    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(hh_cli, "QueueService", FakeQueueService)

    assert (
        hh_cli.run(
            [
                "configure-queue",
                "--daily-limit",
                "30",
                "--delay-min",
                "35",
                "--delay-max",
                "55",
            ]
        )
        == 0
    )
    assert "Суточное ограничение: 30" in capsys.readouterr().out
    assert hh_cli.run(["pause"]) == 0
    assert "Очередь: приостановлена" in capsys.readouterr().out
    assert [name for name, _ in calls] == ["configure", "pause"]
    assert database.closed


def test_rejected_card_and_restore_commands_work_without_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = FakeDatabase()
    vacancy = SimpleNamespace(
        hh_id="123",
        title="Python-разработчик",
        employer_name="Компания",
        source_url="https://hh.ru/vacancy/123",
        availability=VacancyAvailability.ACTIVE,
        region="Москва",
        address="ул. Примерная, 1",
        salary_from=Decimal("120000"),
        salary_to=Decimal("180000"),
        salary_currency="RUR",
        work_format="Удалённо",
        employment="Полная занятость",
        schedule="5/2",
        experience="1-3 года",
        key_skills=("Python", "FastAPI"),
        duplicate_of_id=None,
        description="Разработка API",
    )
    tracking = SimpleNamespace(
        state=VacancyState.FILTERED_OUT,
        rules_score=42.0,
        rules_details={"reasons": ["причина отклонения"]},
    )
    entry = SimpleNamespace(
        vacancy=vacancy,
        tracking=tracking,
        discoveries=(
            SimpleNamespace(
                query_text="Python",
                region="Москва",
                discovered_at=datetime(2026, 7, 22, tzinfo=UTC),
            ),
        ),
        changes=(SimpleNamespace(),),
    )
    calls: list[tuple[str, object]] = []

    class FakeReviewService:
        def __init__(self, session: object) -> None:
            assert session is not None

        def list_rejected(self, **kwargs: object) -> tuple[SimpleNamespace, ...]:
            calls.append(("list", kwargs))
            return (entry,)

        def get_card(self, **kwargs: object) -> SimpleNamespace:
            calls.append(("card", kwargs))
            return entry

        def restore(self, **kwargs: object) -> SimpleNamespace:
            calls.append(("restore", kwargs))
            return entry

    class FakeAutomationService:
        def __init__(self, session: object) -> None:
            assert session is not None

        def prepare_for_account_id(self, **kwargs: object) -> SimpleNamespace:
            assert kwargs["account_id"] == 1
            assert kwargs["direction_name"] == "ИТ"
            return SimpleNamespace(created=1)

    install_fakes(monkeypatch, tmp_path, FakeStore())
    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(hh_cli, "VacancyReviewService", FakeReviewService)
    monkeypatch.setattr(hh_cli, "ApplicationAutomationService", FakeAutomationService)

    assert (
        hh_cli.run(
            [
                "rejected",
                "--direction",
                "ИТ",
                "--company",
                "Компания",
                "--region",
                "Москва",
                "--reason",
                "причина",
                "--sort",
                "score",
            ]
        )
        == 0
    )
    rejected_output = capsys.readouterr().out
    assert "Отклонённых вакансий: 1" in rejected_output
    assert "123: Python-разработчик" in rejected_output

    assert hh_cli.run(["vacancy", "--direction", "ИТ", "--vacancy-id", "123"]) == 0
    card_output = capsys.readouterr().out
    assert "Зарплата: 120000" in card_output
    assert "180000 RUR" in card_output
    assert "Найдена по:" in card_output
    assert "Описание:" in card_output

    assert hh_cli.run(["restore", "--direction", "ИТ", "--vacancy-id", "123"]) == 0
    assert "возвращена в очередь" in capsys.readouterr().out
    assert [name for name, _ in calls] == ["list", "card", "restore"]
    assert database.closed


def test_search_tasks_support_manual_and_saved_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(environment="test")
    manual = hh_cli._search_tasks(
        argparse.Namespace(
            query=["Python", "FastAPI"],
            area=["1", "3"],
        ),
        settings,
    )
    assert [(task.query, task.area) for task in manual] == [
        ("Python", "1"),
        ("Python", "3"),
        ("FastAPI", "1"),
        ("FastAPI", "3"),
    ]
    default_area = hh_cli._search_tasks(
        argparse.Namespace(query=["Python"], area=None),
        settings,
    )
    assert default_area[0].region_name == "Россия"
    with pytest.raises(ValueError, match="--query"):
        hh_cli._search_tasks(argparse.Namespace(query=None, area=["1"]), settings)

    database = FakeDatabase()
    saved_task = VacancySearchTask(1, "Python", "3", "Екатеринбург", {})

    class FakeDirectionService:
        def __init__(self, session: object) -> None:
            pass

        def build_search_tasks(
            self, account_id: int, direction_name: str
        ) -> tuple[VacancySearchTask, ...]:
            assert (account_id, direction_name) == (2, "ИТ")
            return (saved_task,)

    monkeypatch.setattr(hh_cli, "upgrade_database", lambda value: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda value: database)
    monkeypatch.setattr(hh_cli, "CareerDirectionService", FakeDirectionService)
    saved = hh_cli._search_tasks(
        argparse.Namespace(query=None, area=None, account_id=2, direction="ИТ"),
        settings,
    )
    assert saved == (saved_task,)
    assert database.closed


def test_print_search_settings(capsys: pytest.CaptureFixture[str]) -> None:
    value = SimpleNamespace(
        direction=SimpleNamespace(name="ИТ", id=4),
        resume=SimpleNamespace(title="Python-разработчик"),
        queries=(
            SimpleNamespace(
                query="Python",
                regions=(SearchRegion("1", "Москва"),),
            ),
        ),
        work_formats=(WorkFormat.REMOTE, WorkFormat.HYBRID),
        employment_forms=(EmploymentForm.FULL,),
        minimum_salary=180_000,
        desired_salary=220_000,
        salary_currency="RUB",
        remote_all_russia=True,
        skills_from_resume=("Python",),
    )
    hh_cli._print_search_settings(cast(DirectionSearchSettings, value))
    output = capsys.readouterr().out
    assert "Города: Москва" in output
    assert "Форматы: удалённо, гибрид" in output
    assert "Занятость: полная" in output
    assert "минимум 180000, желаемая 220000 RUB" in output
    assert "включён" in output
    assert "1 блоков" in output

    empty = SimpleNamespace(
        direction=SimpleNamespace(name="ИТ", id=4),
        resume=SimpleNamespace(title="Python-разработчик"),
        queries=(SimpleNamespace(query="Python", regions=()),),
        work_formats=(),
        employment_forms=(),
        minimum_salary=None,
        desired_salary=None,
        salary_currency="RUB",
        remote_all_russia=False,
        skills_from_resume=(),
    )
    hh_cli._print_search_settings(cast(DirectionSearchSettings, empty))
    empty_output = capsys.readouterr().out
    assert "Города: не указаны" in empty_output
    assert "Форматы: без ограничения" in empty_output
    assert "Занятость: без ограничения" in empty_output
    assert "выключен" in empty_output


def test_print_pending_questions(capsys: pytest.CaptureFixture[str]) -> None:
    hh_cli._print_pending_questions(())
    assert capsys.readouterr().out == ""

    hh_cli._print_pending_questions(
        (ProfileQuestionCandidate("salary_expectation", "Какая зарплата?"),)
    )
    output = capsys.readouterr().out
    assert "резюме не найдены" in output
    assert "salary_expectation: Какая зарплата?" in output
    assert "поиск продолжится без соответствующего ограничения" in output


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
            vacancy=SimpleNamespace(title="Python-разработчик", duplicate_of_id=None),
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

    class FakeAutomationService:
        def __init__(self, session: object) -> None:
            assert session is not None

        def prepare(self, **kwargs: object) -> SimpleNamespace:
            assert kwargs["include_stretch"] is True
            return SimpleNamespace(created=1, existing=0)

    monkeypatch.setattr(hh_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(hh_cli, "create_database", lambda settings: database)
    monkeypatch.setattr(
        hh_cli,
        "HhProfileSyncService",
        lambda session: SimpleNamespace(synchronize=lambda profile: None),
    )
    monkeypatch.setattr(hh_cli, "VacancyAnalysisService", FakeAnalysisService)
    monkeypatch.setattr(hh_cli, "ApplicationAutomationService", FakeAutomationService)

    assert hh_cli.run(["analyze", "--direction", "Python backend"]) == 0

    output = capsys.readouterr().out
    assert "Проверено вакансий: 1" in output
    assert "Подходят: 1. Пограничные: 0. Отклонены: 0" in output
    assert "Добавлено в очередь: 1" in output
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

        def recover_interrupted(self) -> int:
            return 0

        def policy(self, timezone_name: str) -> SimpleNamespace:
            assert timezone_name
            return SimpleNamespace(
                daily_limit=25,
                delay_min_seconds=30,
                delay_max_seconds=60,
            )

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

        def record_result(
            self,
            queued_job: object,
            result: HhApplyResult,
            **kwargs: object,
        ) -> SimpleNamespace:
            assert queued_job is job
            assert result.status is HhApplyStatus.APPLIED
            assert kwargs["apply_delay"] is not None
            return SimpleNamespace(sent=True, blocking=False, next_apply_at=None)

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


def test_apply_keeps_queue_available_when_one_result_is_unknown(
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

        def recover_interrupted(self) -> int:
            return 1

        def policy(self, timezone_name: str) -> SimpleNamespace:
            return SimpleNamespace(
                daily_limit=25,
                delay_min_seconds=30,
                delay_max_seconds=60,
            )

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

        def record_result(
            self,
            queued_job: object,
            result: HhApplyResult,
            **kwargs: object,
        ) -> SimpleNamespace:
            assert result.status is HhApplyStatus.UNKNOWN_RESULT
            assert result.confirmation == "Ошибка выполнения: RuntimeError"
            assert kwargs["apply_delay"] is None
            return SimpleNamespace(sent=False, blocking=False, next_apply_at=None)

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

    assert hh_cli.run(["apply", "--direction", "Python backend", "--limit", "1"]) == 0


def test_main_uses_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hh_cli, "run", lambda: 2)

    with pytest.raises(SystemExit) as error:
        hh_cli.main()

    assert error.value.code == 2
