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
    INVALID_CREDENTIALS = "invalid_credentials"
    MANUAL_ACTION_REQUIRED = "manual_action_required"


class CredentialStore(Protocol):
    def load(self, account_id: int) -> HhCredentials | None: ...


class HhLoginBrowser(Protocol):
    def open_login(self) -> None: ...

    def is_authenticated(self) -> bool: ...

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
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")

        browser.open_login()
        if browser.is_authenticated():
            return LoginResult(LoginStatus.AUTHENTICATED)

        credentials = self._credentials.load(account_id)
        if credentials is None:
            return LoginResult(LoginStatus.CREDENTIALS_REQUIRED)

        return LoginResult(browser.submit_credentials(credentials))
