from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from hugin.domain.hh_sync import HhSyncRetryableError
from hugin.services.hh_login import HhCredentials, HhLoginService, LoginStatus


@dataclass
class FakeStore:
    credentials: HhCredentials | None
    loaded_account_id: int | None = None
    deleted_account_ids: list[int] = field(default_factory=list)

    def load(self, account_id: int) -> HhCredentials | None:
        self.loaded_account_id = account_id
        return self.credentials

    def delete(self, account_id: int) -> bool:
        self.deleted_account_ids.append(account_id)
        deleted = self.credentials is not None
        self.credentials = None
        return deleted


@dataclass
class FakeBrowser:
    authenticated: bool = False
    result: LoginStatus = LoginStatus.MANUAL_ACTION_REQUIRED
    current_status: LoginStatus = LoginStatus.MANUAL_ACTION_REQUIRED
    opened: bool = False
    submitted: HhCredentials | None = None
    waited: bool = False

    def open_login(self) -> None:
        self.opened = True

    def is_authenticated(self) -> bool:
        return self.authenticated

    def authentication_status(self) -> LoginStatus:
        if self.authenticated:
            return LoginStatus.AUTHENTICATED
        return self.current_status

    def wait_for_authentication(self) -> bool:
        self.waited = True
        return self.authenticated

    def submit_credentials(self, credentials: HhCredentials) -> LoginStatus:
        self.submitted = credentials
        self.current_status = self.result
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


def test_invalid_saved_password_is_deleted_and_not_submitted_again() -> None:
    credentials = HhCredentials("person@example.com", "wrong-secret")
    store = FakeStore(credentials)
    first_browser = FakeBrowser(result=LoginStatus.INVALID_CREDENTIALS)

    first = HhLoginService(store).authenticate(3, first_browser)

    assert first.status is LoginStatus.INVALID_CREDENTIALS
    assert first_browser.submitted == credentials
    assert store.deleted_account_ids == [3]
    assert store.credentials is None

    second_browser = FakeBrowser()
    second = HhLoginService(store).authenticate(3, second_browser)

    assert second.status is LoginStatus.CREDENTIALS_REQUIRED
    assert second_browser.submitted is None
    assert store.deleted_account_ids == [3]


def test_manual_challenge_completion_is_detected_without_extra_confirmation() -> None:
    credentials = HhCredentials("person@example.com", "secret")
    store = FakeStore(credentials)

    class WaitingBrowser(FakeBrowser):
        waited = False

        def wait_for_authentication(self) -> bool:
            self.waited = True
            self.authenticated = True
            return True

    browser = WaitingBrowser(result=LoginStatus.CAPTCHA_REQUIRED)

    result = HhLoginService(store).authenticate(3, browser)

    assert result.status is LoginStatus.AUTHENTICATED
    assert browser.waited


@pytest.mark.parametrize(
    "status",
    [LoginStatus.CAPTCHA_REQUIRED, LoginStatus.CONFIRMATION_REQUIRED],
)
def test_existing_challenge_is_observed_without_resending_credentials(
    status: LoginStatus,
) -> None:
    store = FakeStore(HhCredentials("person@example.com", "secret"))
    browser = FakeBrowser(current_status=status)

    result = HhLoginService(store).authenticate(3, browser)

    assert result.status is status
    assert browser.waited
    assert not browser.opened
    assert browser.submitted is None
    assert store.loaded_account_id is None


def test_login_page_timeout_does_not_load_or_submit_credentials() -> None:
    store = FakeStore(HhCredentials("person@example.com", "secret"))

    class TimeoutBrowser(FakeBrowser):
        def open_login(self) -> None:
            self.opened = True
            raise HhSyncRetryableError(
                "HH_NETWORK_TIMEOUT",
                "Страница входа временно недоступна",
                retry_after_seconds=60,
            )

    browser = TimeoutBrowser()

    with pytest.raises(HhSyncRetryableError):
        HhLoginService(store).authenticate(3, browser)

    assert browser.opened
    assert browser.submitted is None
    assert store.loaded_account_id is None


def test_observation_never_loads_or_submits_credentials() -> None:
    store = FakeStore(HhCredentials("person@example.com", "secret"))
    browser = FakeBrowser(current_status=LoginStatus.CAPTCHA_REQUIRED)

    result = HhLoginService(store).observe_authentication(3, browser)

    assert result.status is LoginStatus.CAPTCHA_REQUIRED
    assert not browser.opened
    assert browser.waited
    assert browser.submitted is None
    assert store.loaded_account_id is None


def test_account_warning_stops_authentication_without_credentials() -> None:
    store = FakeStore(HhCredentials("person@example.com", "secret"))
    browser = FakeBrowser(current_status=LoginStatus.ACCOUNT_WARNING)

    result = HhLoginService(store).authenticate(3, browser)

    assert result.status is LoginStatus.ACCOUNT_WARNING
    assert browser.submitted is None
    assert store.loaded_account_id is None
    assert not browser.waited


def test_account_id_must_be_positive() -> None:
    with pytest.raises(ValueError):
        HhLoginService(FakeStore(None)).authenticate(0, FakeBrowser())
