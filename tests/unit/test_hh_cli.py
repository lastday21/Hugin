from __future__ import annotations

import getpass
from pathlib import Path
from types import TracebackType

import pytest

from hugin import hh_cli
from hugin.core.settings import Settings
from hugin.services.hh_login import HhCredentials, LoginStatus


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

    def __init__(self, profile_dir: object, login_url: str, timeout_ms: int) -> None:
        self.profile_dir = profile_dir
        self.login_url = login_url
        self.timeout_ms = timeout_ms
        self.opened = False
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
    assert str(FakeBrowser.created.profile_dir).endswith("browser-profiles\\account-2")


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


def test_positive_account_id_parser() -> None:
    assert hh_cli.positive_int("3") == 3
    with pytest.raises(Exception, match="положительным"):
        hh_cli.positive_int("0")


def test_main_uses_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hh_cli, "run", lambda: 2)

    with pytest.raises(SystemExit) as error:
        hh_cli.main()

    assert error.value.code == 2
