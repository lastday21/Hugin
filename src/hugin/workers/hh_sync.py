from __future__ import annotations

import threading
from collections.abc import Callable

from hugin.adapters.codex_cli import configured_codex_cli_client
from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.adapters.hh_messages import HhBrowserMessageSender
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.domain.automation import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
)
from hugin.domain.content import RecruiterMessageState
from hugin.domain.hh_sync import (
    HhChatMessageData,
    HhNegotiationData,
    HhSyncBlockedError,
    HhSyncRetryableError,
)
from hugin.domain.tasks import SystemState
from hugin.repositories.tasks import SystemStateRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.automation import AutomationSchedulerService
from hugin.services.autonomous_replies import (
    ApprovedReplyToSend,
    AutonomousReplyService,
)
from hugin.services.communications import CommunicationService
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.hh_sync import HhSynchronizationService
from hugin.workers.automation import (
    AutomationJobBlocked,
    AutomationJobDeferred,
    AutomationJobRetry,
)

type ApplicationWorkPending = Callable[[], bool]

_BACKGROUND_PROFILE_LOCK_TIMEOUT_SECONDS = 2.0
_BACKGROUND_PROFILE_RETRY_SECONDS = 15

AUTHENTICATION_ERROR_CODES = frozenset(
    {
        "AUTH_REQUIRED",
        "CAPTCHA_REQUIRED",
        "CONFIRMATION_REQUIRED",
        "CREDENTIALS_REQUIRED",
        "INVALID_CREDENTIALS",
        "MANUAL_ACTION_REQUIRED",
    }
)
AUTHENTICATION_SYSTEM_STATES = frozenset(
    {
        SystemState.AUTH_REQUIRED,
        SystemState.CAPTCHA_REQUIRED,
    }
)


class HhSyncJobHandler:
    def __init__(
        self,
        settings: Settings,
        kind: AutomationJobKind,
        *,
        account_id: int = 1,
        browser_lock: threading.Lock | None = None,
        application_work_pending: ApplicationWorkPending | None = None,
    ) -> None:
        if kind not in {AutomationJobKind.MESSAGES, AutomationJobKind.STATUSES}:
            raise ValueError("Обработчик поддерживает только сообщения и статусы hh.ru")
        self.kind = kind
        self._settings = settings
        self._account_id = account_id
        self._browser_lock = browser_lock or threading.Lock()
        self._application_work_pending = application_work_pending

    def __call__(self, job: AutomationJobRecord) -> AutomationJobResult:
        if job.kind is not self.kind or job.search_query_id is not None:
            raise ValueError("Обработчик получил неподходящее фоновое задание")
        if job.account_id != self._account_id:
            raise ValueError("Фоновое задание относится к другому аккаунту")
        self._defer_for_application()

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
                    self._protect_system(self._system_state(login.status))
                    raise AutomationJobBlocked(
                        login.status.value.upper(),
                        self._login_message(login.status),
                    )
                self._defer_for_application()
                if self.kind is AutomationJobKind.MESSAGES:
                    messages = browser.read_recruiter_messages(vacancy_ids)
                else:
                    statuses = browser.read_application_statuses()
            if self.kind is AutomationJobKind.MESSAGES:
                synchronized = self._synchronize_messages(
                    messages,
                    allow_replies=(
                        job.last_result.get("message_baseline_initialized") is True
                    ),
                )
                return {
                    **synchronized,
                    "message_baseline_initialized": True,
                }
            return self._synchronize_statuses(statuses)
        except HhSyncBlockedError as error:
            self._protect_system(self._system_state_from_code(error.code))
            raise AutomationJobBlocked(error.code, str(error)) from error
        except HhSyncRetryableError as error:
            raise AutomationJobRetry(
                error.code,
                str(error),
                retry_after_seconds=error.retry_after_seconds,
            ) from error
        except RuntimeError as error:
            if "Профиль hh.ru занят другой задачей" not in str(error):
                raise
            raise AutomationJobDeferred(
                "BROWSER_PROFILE_BUSY",
                "Профиль hh.ru занят; фоновая проверка быстро уступила очередь откликам",
                retry_after_seconds=_BACKGROUND_PROFILE_RETRY_SECONDS,
            ) from error

    def _defer_for_application(self) -> None:
        if self._application_work_pending is not None and self._application_work_pending():
            raise AutomationJobDeferred(
                "APPLICATION_READY",
                "Сначала отправляется готовый отклик",
                retry_after_seconds=60,
            )

    def recover_authentication(self) -> bool:
        if not self._browser_lock.acquire(blocking=False):
            return False
        try:
            system_state = self._authentication_system_state()
            if system_state not in AUTHENTICATION_SYSTEM_STATES:
                return False
            with VisibleHhBrowser(
                self._settings.browser_profile_dir(self._account_id),
                self._settings.hh_login_url,
                self._settings.hh_resumes_url,
                self._settings.hh_search_url,
                self._settings.hh_browser_timeout_ms,
                start_minimized=False,
                browser_source_ip=(
                    str(self._settings.hh_browser_source_ip)
                    if self._settings.hh_browser_source_ip is not None
                    else None
                ),
            ) as browser:
                login_service = HhLoginService(WindowsCredentialStore())
                login = (
                    login_service.observe_authentication(self._account_id, browser)
                    if system_state is SystemState.CAPTCHA_REQUIRED
                    else login_service.authenticate(self._account_id, browser)
                )
                if login.status is LoginStatus.ACCOUNT_WARNING:
                    self._protect_system(SystemState.ACCOUNT_WARNING)
                    return False
                if not login.authenticated:
                    target = self._system_state(login.status)
                    if target is not system_state:
                        self._protect_system(target)
                    return False
            return self._restore_after_authentication()
        finally:
            self._browser_lock.release()

    def _authentication_system_state(self) -> SystemState:
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                return SystemStateRepository(session).get().state
        finally:
            database.close()

    def _restore_after_authentication(self) -> bool:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                system = SystemStateRepository(session)
                if system.lock().state not in AUTHENTICATION_SYSTEM_STATES:
                    return False
                ApplicationAutomationService(session).resume_after_authentication()
                scheduler = AutomationSchedulerService(session)
                for job in scheduler.list_for_account(self._account_id):
                    if (
                        job.state is AutomationJobState.BLOCKED
                        and (job.last_error_code or "").strip().upper()
                        in AUTHENTICATION_ERROR_CODES
                    ):
                        scheduler.unblock(job.key)
            return True
        finally:
            database.close()

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
        *,
        allow_replies: bool = True,
    ) -> AutomationJobResult:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                synchronization = HhSynchronizationService(session)
                if not allow_replies:
                    return synchronization.synchronize_messages(
                        account_id=self._account_id,
                        messages=messages,
                    )
                synchronized = synchronization.synchronize_messages_with_new_ids(
                    account_id=self._account_id,
                    messages=messages,
                )
                result = synchronized.metrics
                batch = AutonomousReplyService(session).prepare(
                    account_id=self._account_id,
                    incoming_message_ids=synchronized.new_incoming_message_ids,
                    include_backlog=True,
                    model_factory=lambda: configured_codex_cli_client(
                        self._settings,
                        operation="recruiter_reply",
                    ),
                )
        finally:
            database.close()
        send_metrics = self._send_approved_replies(batch.approved)
        return {
            **result,
            "reply_drafts": batch.drafts_created,
            "approved_replies": len(batch.approved),
            "replies_sent": send_metrics["sent"],
            "replies_failed": send_metrics["failed"] + batch.failed,
            "replies_unknown": send_metrics["unknown"],
            "replies_cancelled": send_metrics["cancelled"],
            "replies_manual": batch.skipped_manual,
        }

    def _send_approved_replies(
        self,
        approved_replies: tuple[ApprovedReplyToSend, ...],
    ) -> dict[str, int]:
        if not approved_replies:
            return {"sent": 0, "failed": 0, "unknown": 0, "cancelled": 0}
        sent = 0
        failed = 0
        unknown = 0
        cancelled = 0
        with (
            self._browser_lock,
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
                self._protect_system(self._system_state(login.status))
                raise AutomationJobBlocked(
                    login.status.value.upper(),
                    self._login_message(login.status),
                )
            for approved in approved_replies:
                database = create_database(self._settings)
                try:
                    with database.sessions.begin() as session:
                        if not AutonomousReplyService(session).approved_for_send(
                            account_id=self._account_id,
                            message_id=approved.message_id,
                            content_version=approved.content_version,
                            content_hash=approved.content_hash,
                        ):
                            cancelled += 1
                            continue
                        message = CommunicationService(
                            session,
                            HhBrowserMessageSender(browser, approved.source_url),
                        ).send_confirmed(
                            account_id=self._account_id,
                            message_id=approved.message_id,
                            content_version=approved.content_version,
                            content_hash=approved.content_hash,
                        )
                finally:
                    database.close()
                if message.state is RecruiterMessageState.SENT:
                    sent += 1
                elif message.state is RecruiterMessageState.UNKNOWN_RESULT:
                    unknown += 1
                else:
                    failed += 1
        return {
            "sent": sent,
            "failed": failed,
            "unknown": unknown,
            "cancelled": cancelled,
        }

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
                current = repository.get().state
                if current is target:
                    return
                if (
                    current in {SystemState.RUNNING, SystemState.PAUSED}
                    and target
                    in {
                        SystemState.AUTH_REQUIRED,
                        SystemState.CAPTCHA_REQUIRED,
                        SystemState.ACCOUNT_WARNING,
                    }
                ) or (
                    current is SystemState.AUTH_REQUIRED
                    and target
                    in {
                        SystemState.CAPTCHA_REQUIRED,
                        SystemState.ACCOUNT_WARNING,
                    }
                ) or (
                    current is SystemState.CAPTCHA_REQUIRED
                    and target is SystemState.ACCOUNT_WARNING
                ):
                    repository.transition(target)
        finally:
            database.close()

    @staticmethod
    def _system_state(status: LoginStatus) -> SystemState:
        if status is LoginStatus.ACCOUNT_WARNING:
            return SystemState.ACCOUNT_WARNING
        if status in {
            LoginStatus.CAPTCHA_REQUIRED,
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.MANUAL_ACTION_REQUIRED,
        }:
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
            LoginStatus.ACCOUNT_WARNING: "hh.ru показал предупреждение безопасности аккаунта",
            LoginStatus.INVALID_CREDENTIALS: "hh.ru отклонил логин или пароль",
            LoginStatus.MANUAL_ACTION_REQUIRED: "Завершите вход в открытом окне браузера",
            LoginStatus.AUTHENTICATED: "Вход в hh.ru выполнен",
        }
        return messages[status]
