from __future__ import annotations

import threading

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.domain.automation import AutomationJobKind, AutomationJobRecord, AutomationJobResult
from hugin.domain.hh_sync import HhChatMessageData, HhNegotiationData, HhSyncBlockedError
from hugin.domain.tasks import SystemState
from hugin.repositories.tasks import SystemStateRepository
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.hh_sync import HhSynchronizationService
from hugin.workers.automation import AutomationJobBlocked


class HhSyncJobHandler:
    def __init__(
        self,
        settings: Settings,
        kind: AutomationJobKind,
        *,
        account_id: int = 1,
        browser_lock: threading.Lock | None = None,
    ) -> None:
        if kind not in {AutomationJobKind.MESSAGES, AutomationJobKind.STATUSES}:
            raise ValueError("Обработчик поддерживает только сообщения и статусы hh.ru")
        self.kind = kind
        self._settings = settings
        self._account_id = account_id
        self._browser_lock = browser_lock or threading.Lock()

    def __call__(self, job: AutomationJobRecord) -> AutomationJobResult:
        if job.kind is not self.kind or job.search_query_id is not None:
            raise ValueError("Обработчик получил неподходящее фоновое задание")
        if job.account_id != self._account_id:
            raise ValueError("Фоновое задание относится к другому аккаунту")

        vacancy_ids = self._tracked_vacancy_ids()
        try:
            with (
                self._browser_lock,
                VisibleHhBrowser(
                    self._settings.browser_profile_dir(self._account_id),
                    self._settings.hh_login_url,
                    self._settings.hh_resumes_url,
                    self._settings.hh_search_url,
                    self._settings.hh_browser_timeout_ms,
                ) as browser,
            ):
                login = HhLoginService(WindowsCredentialStore()).authenticate(
                    self._account_id,
                    browser,
                )
                if not login.authenticated:
                    self._protect_system(self._system_state(login.status))
                    raise AutomationJobBlocked(
                        login.status.value.upper(),
                        self._login_message(login.status),
                    )
                if self.kind is AutomationJobKind.MESSAGES:
                    messages = browser.read_recruiter_messages(vacancy_ids)
                    return self._synchronize_messages(messages)
                statuses = browser.read_application_statuses()
                return self._synchronize_statuses(statuses)
        except HhSyncBlockedError as error:
            self._protect_system(self._system_state_from_code(error.code))
            raise AutomationJobBlocked(error.code, str(error)) from error

    def _tracked_vacancy_ids(self) -> tuple[str, ...]:
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                return HhSynchronizationService(session).tracked_vacancy_ids(self._account_id)
        finally:
            database.close()

    def _synchronize_messages(
        self,
        messages: tuple[HhChatMessageData, ...],
    ) -> AutomationJobResult:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                return HhSynchronizationService(session).synchronize_messages(
                    account_id=self._account_id,
                    messages=messages,
                )
        finally:
            database.close()

    def _synchronize_statuses(
        self,
        statuses: tuple[HhNegotiationData, ...],
    ) -> AutomationJobResult:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                return HhSynchronizationService(session).synchronize_statuses(
                    account_id=self._account_id,
                    statuses=statuses,
                )
        finally:
            database.close()

    def _protect_system(self, target: SystemState) -> None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                repository = SystemStateRepository(session)
                if repository.get().state is SystemState.RUNNING:
                    repository.transition(target)
        finally:
            database.close()

    @staticmethod
    def _system_state(status: LoginStatus) -> SystemState:
        if status is LoginStatus.CAPTCHA_REQUIRED:
            return SystemState.CAPTCHA_REQUIRED
        return SystemState.AUTH_REQUIRED

    @staticmethod
    def _system_state_from_code(code: str) -> SystemState:
        states = {
            "CAPTCHA_REQUIRED": SystemState.CAPTCHA_REQUIRED,
            "ACCOUNT_WARNING": SystemState.ACCOUNT_WARNING,
        }
        return states.get(code, SystemState.AUTH_REQUIRED)

    @staticmethod
    def _login_message(status: LoginStatus) -> str:
        messages = {
            LoginStatus.CREDENTIALS_REQUIRED: "Сначала сохраните данные входа hh.ru",
            LoginStatus.CONFIRMATION_REQUIRED: "Введите код в открытом окне браузера",
            LoginStatus.CAPTCHA_REQUIRED: "Пройдите проверку hh.ru в открытом окне",
            LoginStatus.INVALID_CREDENTIALS: "hh.ru отклонил логин или пароль",
            LoginStatus.MANUAL_ACTION_REQUIRED: "Завершите вход в открытом окне браузера",
            LoginStatus.AUTHENTICATED: "Вход в hh.ru выполнен",
        }
        return messages[status]
