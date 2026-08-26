from __future__ import annotations

import threading
from collections.abc import Callable

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings
from hugin.domain.automation import AutomationJobKind, AutomationJobRecord, AutomationJobResult
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.search_cycle import BackgroundSearchCycle
from hugin.workers.automation import (
    AutomationJobBlocked,
    AutomationJobDeferred,
    background_browser_access,
)

type ApplicationWorkPending = Callable[[], bool]

_BACKGROUND_PROFILE_LOCK_TIMEOUT_SECONDS = 2.0
_BACKGROUND_PROFILE_RETRY_SECONDS = 15


class HhSearchJobHandler:
    kind = AutomationJobKind.SEARCH

    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        browser_lock: threading.Lock | None = None,
        application_work_pending: ApplicationWorkPending | None = None,
    ) -> None:
        self._settings = settings
        self._account_id = account_id
        self._browser_lock = browser_lock or threading.Lock()
        self._application_work_pending = application_work_pending
        self._cycle = BackgroundSearchCycle(
            settings,
            page_limit=settings.hh_background_search_pages,
            detail_limit=settings.hh_background_detail_limit,
        )

    def __call__(self, job: AutomationJobRecord) -> AutomationJobResult:
        if job.kind is not self.kind or job.search_query_id is None:
            raise ValueError("Обработчик получил не поисковое задание")
        if job.account_id != self._account_id:
            raise ValueError("Фоновое задание относится к другому аккаунту")
        if self._application_work_pending is not None and self._application_work_pending():
            raise AutomationJobDeferred(
                "APPLICATIONS_PENDING",
                "Сначала обрабатываются уже найденные вакансии",
                retry_after_seconds=60,
            )

        try:
            with (
                background_browser_access(
                    self._browser_lock,
                    timeout_seconds=_BACKGROUND_PROFILE_LOCK_TIMEOUT_SECONDS,
                    message=("Профиль hh.ru занят; фоновый поиск быстро уступил очередь откликам"),
                    retry_after_seconds=_BACKGROUND_PROFILE_RETRY_SECONDS,
                ),
                VisibleHhBrowser(
                    self._settings.browser_profile_dir(self._account_id),
                    self._settings.hh_login_url,
                    self._settings.hh_resumes_url,
                    self._settings.hh_search_url,
                    self._settings.hh_browser_timeout_ms,
                    start_minimized=True,
                    browser_source_ip=(
                        str(self._settings.hh_browser_source_ip)
                        if self._settings.hh_browser_source_ip is not None
                        else None
                    ),
                    profile_lock_timeout_seconds=_BACKGROUND_PROFILE_LOCK_TIMEOUT_SECONDS,
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
        except RuntimeError as error:
            if "Профиль hh.ru занят другой задачей" not in str(error):
                raise
            raise AutomationJobDeferred(
                "BROWSER_PROFILE_BUSY",
                "Профиль hh.ru занят; фоновый поиск быстро уступил очередь откликам",
                retry_after_seconds=_BACKGROUND_PROFILE_RETRY_SECONDS,
            ) from error

    @staticmethod
    def _login_message(status: LoginStatus) -> str:
        messages = {
            LoginStatus.CREDENTIALS_REQUIRED: "Сначала сохраните данные входа hh.ru",
            LoginStatus.CONFIRMATION_REQUIRED: "Введите код в открытом окне браузера",
            LoginStatus.CAPTCHA_REQUIRED: "Пройдите проверку hh.ru в открытом окне",
            LoginStatus.ACCOUNT_WARNING: "hh.ru показал предупреждение безопасности аккаунта",
            LoginStatus.INVALID_CREDENTIALS: "hh.ru отклонил логин или пароль",
            LoginStatus.MANUAL_ACTION_REQUIRED: "Завершите вход в открытом окне браузера",
            LoginStatus.AUTHENTICATED: "Вход в hh.ru выполнен",
        }
        return messages[status]
