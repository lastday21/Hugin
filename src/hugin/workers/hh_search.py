from __future__ import annotations

import threading

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings
from hugin.domain.automation import AutomationJobKind, AutomationJobRecord, AutomationJobResult
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.search_cycle import BackgroundSearchCycle
from hugin.workers.automation import AutomationJobBlocked


class HhSearchJobHandler:
    kind = AutomationJobKind.SEARCH

    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        browser_lock: threading.Lock | None = None,
    ) -> None:
        self._settings = settings
        self._account_id = account_id
        self._browser_lock = browser_lock or threading.Lock()
        self._cycle = BackgroundSearchCycle(
            settings,
            detail_limit=settings.hh_background_detail_limit,
        )

    def __call__(self, job: AutomationJobRecord) -> AutomationJobResult:
        if job.kind is not self.kind or job.search_query_id is None:
            raise ValueError("Обработчик получил не поисковое задание")
        if job.account_id != self._account_id:
            raise ValueError("Фоновое задание относится к другому аккаунту")

        with (
            self._browser_lock,
            VisibleHhBrowser(
                self._settings.browser_profile_dir(self._account_id),
                self._settings.hh_login_url,
                self._settings.hh_resumes_url,
                self._settings.hh_search_url,
                self._settings.hh_browser_timeout_ms,
                start_minimized=True,
            ) as browser,
        ):
            login = HhLoginService(WindowsCredentialStore()).authenticate(
                self._account_id,
                browser,
            )
            if not login.authenticated:
                raise AutomationJobBlocked(
                    login.status.value.upper(),
                    self._login_message(login.status),
                )
            return self._cycle.run(
                account_id=self._account_id,
                search_query_id=job.search_query_id,
                browser=browser,
            )

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
