from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class HhCredentials:
    login: str
    password: str

    def __post_init__(self) -> None:
        if not self.login.strip():
            raise ValueError("Логин hh.ru не может быть пустым")
        if not self.password:
            raise ValueError("Пароль hh.ru не может быть пустым")

    def __repr__(self) -> str:
        return "HhCredentials(login='***', password='***')"


class LoginStatus(StrEnum):
    AUTHENTICATED = "authenticated"
    CREDENTIALS_REQUIRED = "credentials_required"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CAPTCHA_REQUIRED = "captcha_required"
    ACCOUNT_WARNING = "account_warning"
    INVALID_CREDENTIALS = "invalid_credentials"
    MANUAL_ACTION_REQUIRED = "manual_action_required"


class CredentialStore(Protocol):
    def load(self, account_id: int) -> HhCredentials | None: ...

    def delete(self, account_id: int) -> bool: ...


class HhLoginBrowser(Protocol):
    def open_login(self) -> None: ...

    def is_authenticated(self) -> bool: ...

    def authentication_status(self) -> LoginStatus: ...

    def wait_for_authentication(self) -> bool: ...

    def submit_credentials(self, credentials: HhCredentials) -> LoginStatus: ...


@dataclass(frozen=True, slots=True)
class LoginResult:
    status: LoginStatus

    @property
    def authenticated(self) -> bool:
        return self.status is LoginStatus.AUTHENTICATED


class HhLoginService:
    def __init__(self, credentials: CredentialStore) -> None:
        self._credentials = credentials

    def authenticate(self, account_id: int, browser: HhLoginBrowser) -> LoginResult:
        self._validate_account_id(account_id)

        current_status = self._current_status(browser)
        if current_status not in {
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED,
            LoginStatus.ACCOUNT_WARNING,
        }:
            browser.open_login()
            current_status = self._current_status(browser)
        if current_status in {
            LoginStatus.AUTHENTICATED,
            LoginStatus.ACCOUNT_WARNING,
        }:
            return LoginResult(current_status)
        if current_status in {
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED,
        }:
            return self._wait_for_user(browser, current_status)

        credentials = self._credentials.load(account_id)
        if credentials is None:
            return LoginResult(LoginStatus.CREDENTIALS_REQUIRED)

        status = browser.submit_credentials(credentials)
        if status is LoginStatus.INVALID_CREDENTIALS:
            self._credentials.delete(account_id)
        if status in {
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED,
            LoginStatus.MANUAL_ACTION_REQUIRED,
        }:
            return self._wait_for_user(browser, status)
        return LoginResult(status)

    def observe_authentication(
        self,
        account_id: int,
        browser: HhLoginBrowser,
    ) -> LoginResult:
        self._validate_account_id(account_id)
        status = self._current_status(browser)
        if status not in {
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED,
            LoginStatus.ACCOUNT_WARNING,
        }:
            browser.open_login()
            status = self._current_status(browser)
        if status in {
            LoginStatus.AUTHENTICATED,
            LoginStatus.ACCOUNT_WARNING,
        }:
            return LoginResult(status)
        return self._wait_for_user(browser, status)

    @staticmethod
    def _validate_account_id(account_id: int) -> None:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")

    @staticmethod
    def _current_status(browser: HhLoginBrowser) -> LoginStatus:
        status_reader = getattr(browser, "authentication_status", None)
        if callable(status_reader):
            status = status_reader()
            if isinstance(status, LoginStatus):
                return status
            raise TypeError("Браузер вернул неизвестное состояние входа hh.ru")
        if browser.is_authenticated():
            return LoginStatus.AUTHENTICATED
        return LoginStatus.MANUAL_ACTION_REQUIRED

    @classmethod
    def _wait_for_user(
        cls,
        browser: HhLoginBrowser,
        fallback: LoginStatus,
    ) -> LoginResult:
        wait_for_authentication = getattr(browser, "wait_for_authentication", None)
        if callable(wait_for_authentication) and wait_for_authentication():
            status = cls._current_status(browser)
            if status is LoginStatus.AUTHENTICATED:
                return LoginResult(status)
        status = cls._current_status(browser)
        if status in {
            LoginStatus.AUTHENTICATED,
            LoginStatus.ACCOUNT_WARNING,
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED,
            LoginStatus.INVALID_CREDENTIALS,
        }:
            return LoginResult(status)
        return LoginResult(fallback)
