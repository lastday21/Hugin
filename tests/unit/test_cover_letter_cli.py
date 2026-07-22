from __future__ import annotations

import argparse
import getpass
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr

from hugin import cover_letter_cli
from hugin.adapters.yandex_credentials import (
    WindowsYandexAICredentialStore,
    YandexAICredentials,
)
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CoverLetterModel
from hugin.domain.content import CoverLetterState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, ApplicationRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.cover_letter import (
    CoverLetterPreparationItem,
    CoverLetterPreparationResult,
    CoverLetterStatus,
)


class FakeStore:
    def __init__(self, credentials: YandexAICredentials | None = None) -> None:
        self.credentials = credentials
        self.saved: YandexAICredentials | None = None

    def save(self, credentials: YandexAICredentials) -> None:
        self.saved = credentials

    def load(self) -> YandexAICredentials | None:
        return self.credentials


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


def test_configure_keeps_key_out_of_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore()
    monkeypatch.setattr(cover_letter_cli, "WindowsYandexAICredentialStore", lambda: store)
    monkeypatch.setattr(cover_letter_cli, "get_settings", lambda: Settings())
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "secret-key")

    assert (
        cover_letter_cli.run(["configure", "--folder-id", "folder", "--model", "yandexgpt/latest"])
        == 0
    )
    assert store.saved == YandexAICredentials("secret-key", "folder", "yandexgpt/latest")
    assert "secret-key" not in capsys.readouterr().out


def test_prepare_creates_letters_without_browser_or_hh_submission(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = FakeDatabase()
    prepared = CoverLetterPreparationResult(
        generated=1,
        reused=0,
        already_ready=0,
        failed=0,
        items=(
            CoverLetterPreparationItem(
                application_id=1,
                vacancy_id=2,
                hh_id="123",
                title="Python-разработчик",
                state=CoverLetterState.READY,
                action="generated",
            ),
        ),
    )

    class FakeAutomation:
        def __init__(self, _session: object) -> None:
            pass

        def prepare_for_account_id(self, **kwargs: object) -> SimpleNamespace:
            assert kwargs == {
                "account_id": 1,
                "direction_name": "Python backend",
                "include_stretch": True,
            }
            return SimpleNamespace(created=3, existing=4)

    class FakeLetters:
        def __init__(self, _session: object, client: object | None = None) -> None:
            assert client is not None

        def prepare(self, **kwargs: object) -> CoverLetterPreparationResult:
            assert kwargs == {
                "account_id": 1,
                "direction_name": "Python backend",
                "limit": 5,
                "vacancy_hh_id": "123",
            }
            return prepared

    monkeypatch.setattr(
        cover_letter_cli,
        "WindowsYandexAICredentialStore",
        lambda: FakeStore(YandexAICredentials("key", "folder")),
    )
    monkeypatch.setattr(cover_letter_cli, "get_settings", lambda: Settings())
    monkeypatch.setattr(cover_letter_cli, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(cover_letter_cli, "create_database", lambda _settings: database)
    monkeypatch.setattr(cover_letter_cli, "_client", lambda _settings, _store: object())
    monkeypatch.setattr(cover_letter_cli, "ApplicationAutomationService", FakeAutomation)
    monkeypatch.setattr(cover_letter_cli, "CoverLetterService", FakeLetters)

    assert (
        cover_letter_cli.run(
            [
                "prepare",
                "--direction",
                "Python backend",
                "--limit",
                "5",
                "--vacancy-id",
                "123",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "№ 123" in output
    assert "На hh.ru ничего не отправлено" in output
    assert database.closed


def test_status_does_not_require_yandex_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = FakeDatabase()

    class FakeLetters:
        def __init__(self, _session: object) -> None:
            pass

        def status(self, **kwargs: object) -> CoverLetterStatus:
            assert kwargs["direction_name"] == "Python backend"
            return CoverLetterStatus(ready=2, failed=1, pending=0, missing=3)

    monkeypatch.setattr(cover_letter_cli, "WindowsYandexAICredentialStore", FakeStore)
    monkeypatch.setattr(cover_letter_cli, "get_settings", lambda: Settings())
    monkeypatch.setattr(cover_letter_cli, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(cover_letter_cli, "create_database", lambda _settings: database)
    monkeypatch.setattr(cover_letter_cli, "CoverLetterService", FakeLetters)

    assert cover_letter_cli.run(["status", "--direction", "Python backend"]) == 0
    output = capsys.readouterr().out
    assert "Готово: 2" in output
    assert "Еще не подготовлено: 3" in output
    assert database.closed


def test_connection_check_uses_configured_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = SimpleNamespace(
        model_name="yandexgpt-test",
        complete=lambda _system, _user: "готово",
    )
    monkeypatch.setattr(cover_letter_cli, "WindowsYandexAICredentialStore", FakeStore)
    monkeypatch.setattr(cover_letter_cli, "get_settings", lambda: Settings())
    monkeypatch.setattr(cover_letter_cli, "_client", lambda _settings, _store: client)

    assert cover_letter_cli.run(["test"]) == 0
    assert "yandexgpt-test: готово" in capsys.readouterr().out


def test_show_reads_saved_letter(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Кандидат", "show-account")
            resume = ResumeRepository(session).upsert(account.id, "show-resume", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("show-123", "Python-разработчик", "https://hh.ru/vacancy/show-123")
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            session.add(
                CoverLetterModel(
                    application_id=application.id,
                    vacancy_id=vacancy.id,
                    resume_id=resume.id,
                    text="Сохраненное индивидуальное письмо",
                    instruction_version="cover_letter_v1",
                    model_name="yandexgpt-test",
                    context_hash="hash",
                    state=CoverLetterState.READY,
                )
            )
    finally:
        database.close()

    monkeypatch.setattr(cover_letter_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cover_letter_cli, "WindowsYandexAICredentialStore", FakeStore)

    assert cover_letter_cli.run(["show", "--account-id", "1", "--vacancy-id", "show-123"]) == 0
    output = capsys.readouterr().out
    assert "Python-разработчик" in output
    assert "Сохраненное индивидуальное письмо" in output


def test_client_loads_environment_and_protected_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[object, ...]] = []

    class FakeClient:
        def __init__(self, *args: object) -> None:
            received.append(args)

    monkeypatch.setattr(cover_letter_cli, "YandexAIClient", FakeClient)
    environment = Settings(
        yandex_ai_api_key=SecretStr("env-key"),
        yandex_ai_folder_id="env-folder",
        yandex_ai_model="env-model",
    )
    cover_letter_cli._client(
        environment,
        cast(WindowsYandexAICredentialStore, FakeStore()),
    )
    assert received[-1][:3] == ("env-key", "env-folder", "env-model")

    stored = cast(
        WindowsYandexAICredentialStore,
        FakeStore(YandexAICredentials("stored-key", "stored-folder", "stored-model")),
    )
    cover_letter_cli._client(Settings(), stored)
    assert received[-1][:3] == ("stored-key", "stored-folder", "stored-model")

    with pytest.raises(ValueError, match="HUGIN_YANDEX_AI_FOLDER_ID"):
        cover_letter_cli._client(
            Settings(yandex_ai_api_key=SecretStr("key")),
            cast(WindowsYandexAICredentialStore, FakeStore()),
        )
    with pytest.raises(LookupError, match="hugin-letters configure"):
        cover_letter_cli._client(
            Settings(),
            cast(WindowsYandexAICredentialStore, FakeStore()),
        )


def test_argument_errors_and_main_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cover_letter_cli.positive_int("0")
    monkeypatch.setattr(cover_letter_cli, "run", lambda: 2)
    with pytest.raises(SystemExit) as error:
        cover_letter_cli.main()
    assert error.value.code == 2
