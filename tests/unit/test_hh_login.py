from __future__ import annotations

from dataclasses import dataclass

import pytest

from hugin.services.hh_login import HhCredentials, HhLoginService, LoginStatus


@dataclass
class FakeStore:
    credentials: HhCredentials | None
    loaded_account_id: int | None = None

    def load(self, account_id: int) -> HhCredentials | None:
        self.loaded_account_id = account_id
        return self.credentials


@dataclass
class FakeBrowser:
    authenticated: bool = False
    result: LoginStatus = LoginStatus.MANUAL_ACTION_REQUIRED
    opened: bool = False
    submitted: HhCredentials | None = None

    def open_login(self) -> None:
        self.opened = True

    def is_authenticated(self) -> bool:
        return self.authenticated

    def submit_credentials(self, credentials: HhCredentials) -> LoginStatus:
        self.submitted = credentials
        return self.result


def test_credentials_hide_password_in_representation() -> None:
    credentials = HhCredentials("person@example.com", "secret-value")

    assert "secret-value" not in repr(credentials)
    assert "person@example.com" not in repr(credentials)
    assert "***" in repr(credentials)


@pytest.mark.parametrize(("login", "password"), [("", "secret"), ("   ", "secret"), ("a", "")])
def test_credentials_reject_empty_values(login: str, password: str) -> None:
    with pytest.raises(ValueError):
        HhCredentials(login, password)


def test_existing_browser_session_is_reused_without_loading_password() -> None:
    store = FakeStore(HhCredentials("person@example.com", "secret"))
    browser = FakeBrowser(authenticated=True)

    result = HhLoginService(store).authenticate(1, browser)

    assert result.authenticated
    assert browser.opened
    assert browser.submitted is None
    assert store.loaded_account_id is None


def test_missing_credentials_are_reported() -> None:
    store = FakeStore(None)
    browser = FakeBrowser()

    result = HhLoginService(store).authenticate(2, browser)

    assert result.status is LoginStatus.CREDENTIALS_REQUIRED
    assert not result.authenticated
    assert store.loaded_account_id == 2


def test_credentials_are_submitted_and_browser_status_is_returned() -> None:
    credentials = HhCredentials("person@example.com", "secret")
    store = FakeStore(credentials)
    browser = FakeBrowser(result=LoginStatus.CONFIRMATION_REQUIRED)

    result = HhLoginService(store).authenticate(3, browser)

    assert result.status is LoginStatus.CONFIRMATION_REQUIRED
    assert browser.submitted == credentials


def test_account_id_must_be_positive() -> None:
    with pytest.raises(ValueError):
        HhLoginService(FakeStore(None)).authenticate(0, FakeBrowser())
