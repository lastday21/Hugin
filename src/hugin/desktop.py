from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from queue import Empty, Queue
from typing import Protocol, cast
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from sqlalchemy import select

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.adapters.hh_messages import HhBrowserMessageSender
from hugin.adapters.notification_credentials import load_notification_gateway_credentials
from hugin.adapters.notification_gateway import (
    NotificationGatewayClient,
    NotificationGatewayError,
    NotificationGatewayTimeout,
)
from hugin.adapters.postgres_backup import DockerPostgresBackupAdapter
from hugin.adapters.yandex_ai import YandexAIError
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import ApplicationModel, VacancyModel
from hugin.diagnostics import OperationJournal, error_details
from hugin.domain.automation import AutomationJobKind
from hugin.domain.communications import CommunicationNotFoundError, CommunicationStateError
from hugin.domain.content import RecruiterMessageState
from hugin.domain.hh import HhFormReviewStatus
from hugin.services.ai_prompts import AiPromptSettingsService
from hugin.services.backups import BackupService
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.recruiter_reply import RecruiterReplyService
from hugin.services.screening_forms import ScreeningDraft, ScreeningDraftService
from hugin.services.yandex_client import configured_yandex_ai_client
from hugin.workers.applications import ApplicationWorker
from hugin.workers.automation import AutomationWorker
from hugin.workers.backups import BackupWorker
from hugin.workers.hh_search import HhSearchJobHandler
from hugin.workers.hh_sync import HhSyncJobHandler
from hugin.workers.notifications import NotificationWorker

APP_ICON = Path(__file__).with_name("assets") / "hugin.ico"


class WebviewWindow(Protocol):
    def destroy(self) -> None: ...


class BackgroundWorker(Protocol):
    def start(self) -> None: ...

    def stop(self, timeout_seconds: float = 10.0) -> None: ...


class WebviewModule(Protocol):
    def create_window(
        self,
        title: str,
        url: str,
        *,
        js_api: object,
        width: int,
        height: int,
        min_size: tuple[int, int],
        background_color: str,
    ) -> WebviewWindow: ...

    def start(self, *, debug: bool = False, icon: str | None = None) -> None: ...


class WindowsUser32(Protocol):
    def MessageBoxW(
        self,
        parent: int | None,
        message: str,
        title: str,
        flags: int,
    ) -> int: ...


class WindowsLibraries(Protocol):
    user32: WindowsUser32


def _is_safe_hh_url(value: str) -> bool:
    try:
        target = urlsplit(value)
        port = target.port
    except ValueError:
        return False
    hostname = (target.hostname or "").lower()
    return (
        target.scheme == "https"
        and (hostname == "hh.ru" or hostname.endswith(".hh.ru"))
        and target.username is None
        and target.password is None
        and port in {None, 443}
    )


type _FormReviewOutcome = dict[str, object] | Exception


@dataclass(frozen=True, slots=True)
class _FormReviewCommand:
    draft: ScreeningDraft
    response: Queue[_FormReviewOutcome]


class DesktopBridge:
    def __init__(
        self,
        settings: Settings,
        account_id: int = 1,
        *,
        browser_lock: threading.Lock | None = None,
        journal: OperationJournal | None = None,
    ) -> None:
        self._settings = settings
        self._account_id = account_id
        self._lock = browser_lock or threading.Lock()
        self._telegram_lock = threading.Lock()
        self._journal = journal or OperationJournal(settings.data_dir)
        self._form_review_guard = threading.Lock()
        self._form_review_thread: threading.Thread | None = None
        self._form_review_commands: Queue[_FormReviewCommand | None] | None = None

    def open_form(self, vacancy_id: str) -> dict[str, object]:
        return self._record_action(
            "screening_form.open",
            lambda: self._open_form(vacancy_id.strip()),
            vacancy_id=vacancy_id.strip(),
        )

    def open_url(self, url: str) -> dict[str, object]:
        return self._record_action("hh_link.open", lambda: self._open_url(url))

    def _open_url(self, url: str) -> dict[str, object]:
        value = url.strip()
        if not _is_safe_hh_url(value):
            return self._result("UNAVAILABLE", "Разрешены только ссылки hh.ru")
        opened = webbrowser.open(value, new=2)
        return self._result("READY" if opened else "UNAVAILABLE", "Ссылка открыта")

    def open_invitation(self, invitation_id: int) -> dict[str, object]:
        return self._record_action(
            "invitation.open",
            lambda: self._open_invitation(invitation_id),
            invitation_id=invitation_id,
        )

    def _open_invitation(self, invitation_id: int) -> dict[str, object]:
        if invitation_id < 1:
            return self._result("UNAVAILABLE", "Некорректный номер приглашения")
        upgrade_database(self._settings)
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                invitation = next(
                    (
                        item
                        for item in CommunicationService(
                            session,
                            RecordingMessageSender(),
                        ).invitations(self._account_id)
                        if item.id == invitation_id
                    ),
                    None,
                )
        finally:
            database.close()
        if invitation is None or not invitation.booking_url:
            return self._result("UNAVAILABLE", "Ссылка записи не найдена")
        target = urlsplit(invitation.booking_url.strip())
        if (
            target.scheme != "https"
            or not target.hostname
            or target.username is not None
            or target.password is not None
        ):
            return self._result("UNAVAILABLE", "Ссылка записи небезопасна")
        opened = webbrowser.open(invitation.booking_url.strip(), new=2)
        return self._result("READY" if opened else "UNAVAILABLE", "Ссылка открыта")

    def send_reply(
        self,
        message_id: int,
        content_hash: str,
        content_version: int,
    ) -> dict[str, object]:
        return self._record_action(
            "recruiter_reply.send",
            lambda: self._send_reply(message_id, content_hash, content_version),
            message_id=message_id,
            content_version=content_version,
        )

    def _send_reply(
        self,
        message_id: int,
        content_hash: str,
        content_version: int,
    ) -> dict[str, object]:
        if message_id < 1 or content_version < 1:
            return self._result("UNAVAILABLE", "Некорректная версия сообщения")
        selected_hash = content_hash.strip().lower()
        if len(selected_hash) != 64:
            return self._result("UNAVAILABLE", "Некорректный отпечаток сообщения")
        upgrade_database(self._settings)
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                service = CommunicationService(session, RecordingMessageSender())
                message = next(
                    (item for item in service.messages(self._account_id) if item.id == message_id),
                    None,
                )
                if message is None:
                    return self._result("UNAVAILABLE", "Сообщение не найдено")
                if (
                    message.content_hash != selected_hash
                    or message.content_version != content_version
                ):
                    return self._result(
                        "UNAVAILABLE",
                        "Текст изменился. Проверьте и подтвердите новую версию.",
                    )
                if message.state is not RecruiterMessageState.CONFIRMED:
                    return self._result(
                        "UNAVAILABLE",
                        "Сначала явно подтвердите точный текст ответа.",
                    )
                source_url = session.scalar(
                    select(VacancyModel.source_url)
                    .join(
                        ApplicationModel,
                        ApplicationModel.vacancy_id == VacancyModel.id,
                    )
                    .where(
                        ApplicationModel.id == message.application_id,
                        ApplicationModel.account_id == self._account_id,
                    )
                )
        finally:
            database.close()
        if not source_url:
            return self._result("UNAVAILABLE", "Ссылка на вакансию не найдена")

        with (
            self._lock,
            VisibleHhBrowser(
                self._settings.browser_profile_dir(self._account_id),
                self._settings.hh_login_url,
                self._settings.hh_resumes_url,
                self._settings.hh_search_url,
                self._settings.hh_browser_timeout_ms,
                browser_source_ip=(
                    str(self._settings.hh_browser_source_ip)
                    if self._settings.hh_browser_source_ip is not None
                    else None
                ),
            ) as browser,
        ):
            login = HhLoginService(WindowsCredentialStore()).authenticate(
                self._account_id,
                browser,
            )
            if not login.authenticated:
                return self._result(
                    login.status.value.upper(),
                    "Завершите вход в hh.ru и повторите отправку подтверждённого ответа.",
                )
            database = create_database(self._settings)
            try:
                with database.sessions.begin() as session:
                    sent = CommunicationService(
                        session,
                        HhBrowserMessageSender(browser, source_url),
                    ).send_confirmed(
                        account_id=self._account_id,
                        message_id=message_id,
                        content_version=content_version,
                        content_hash=selected_hash,
                    )
            finally:
                database.close()
        results = {
            RecruiterMessageState.SENT: (
                "SENT",
                "Ответ отправлен и сохранён.",
            ),
            RecruiterMessageState.FAILED: (
                "FAILED",
                "hh.ru отклонил отправку ответа.",
            ),
            RecruiterMessageState.UNKNOWN_RESULT: (
                "UNKNOWN_RESULT",
                "Результат отправки не подтверждён. Повтор заблокирован до сверки.",
            ),
        }
        status, message_text = results.get(
            sent.state,
            ("UNAVAILABLE", "Ответ не был отправлен."),
        )
        return self._result(status, message_text)

    def generate_reply(self, application_id: int) -> dict[str, object]:
        return self._record_action(
            "recruiter_reply.generate",
            lambda: self._generate_reply(application_id),
            application_id=application_id,
        )

    def _generate_reply(self, application_id: int) -> dict[str, object]:
        if application_id < 1:
            return self._result("UNAVAILABLE", "Некорректный номер отклика")
        try:
            upgrade_database(self._settings)
            database = create_database(self._settings)
            try:
                with database.sessions.begin() as session:
                    ai_settings = AiPromptSettingsService(session)
                    client = configured_yandex_ai_client(
                        self._settings,
                        model=ai_settings.get_model(),
                        reasoning_effort=ai_settings.get_reasoning_effort(),
                        operation="recruiter_reply",
                    )
                    draft = RecruiterReplyService(session, client).generate(
                        account_id=self._account_id,
                        application_id=application_id,
                    )
            finally:
                database.close()
        except (
            CommunicationNotFoundError,
            CommunicationStateError,
            LookupError,
            ValueError,
            YandexAIError,
        ) as error:
            return self._result("UNAVAILABLE", str(error))
        return self._result(
            "READY",
            "Черновик подготовлен. Проверьте и при необходимости измените его.",
            body=draft.body,
        )

    def notification_credentials_status(self) -> dict[str, object]:
        try:
            client = self._notification_gateway_client()
        except (OSError, RuntimeError, ValueError) as error:
            return {
                **self._result("READY", str(error)),
                "service_available": False,
                "key_configured": False,
                "telegram": None,
                "paired": None,
                "email": None,
                "telegram_bot_username": self._settings.telegram_bot_username,
            }
        try:
            gateway_status = client.status()
        except NotificationGatewayError as error:
            return {
                **self._result("READY", str(error)),
                "service_available": False,
                "key_configured": True,
                "telegram": None,
                "paired": None,
                "email": None,
                "telegram_bot_username": self._settings.telegram_bot_username,
            }
        return {
            **self._result("READY", "Состояние службы уведомлений проверено"),
            "service_available": gateway_status.available,
            "key_configured": True,
            "telegram": gateway_status.telegram,
            "paired": gateway_status.paired,
            "email": gateway_status.email,
            "telegram_bot_username": self._settings.telegram_bot_username,
        }

    def connect_telegram_notifications(self) -> dict[str, object]:
        return self._record_action(
            "telegram.chat.connect",
            self._connect_telegram_with_lock,
            connection_mode="telegram_start",
        )

    def _connect_telegram_with_lock(self) -> dict[str, object]:
        if not self._telegram_lock.acquire(blocking=False):
            return self._result("BUSY", "Подключение Telegram уже выполняется")
        try:
            return self._connect_telegram_notifications()
        finally:
            self._telegram_lock.release()

    def test_telegram_notifications(self) -> dict[str, object]:
        return self._record_action(
            "telegram.notification.test",
            self._test_telegram_notifications,
        )

    def test_email_notifications(self) -> dict[str, object]:
        return self._record_action(
            "email.notification.test",
            self._test_email_notifications,
        )

    def _test_telegram_notifications(self) -> dict[str, object]:
        try:
            self._notification_gateway_client().send(
                event_id=f"test.telegram.{uuid4().hex}",
                channel="TELEGRAM",
                event_type="DAILY_SUMMARY",
                title="Проверка Telegram",
                body="Hugin успешно отправляет уведомления в Telegram.",
            )
        except (OSError, RuntimeError, ValueError) as error:
            return self._result("UNAVAILABLE", str(error))
        return self._result(
            "READY",
            "Проверочное уведомление отправлено в Telegram.",
        )

    def _test_email_notifications(self) -> dict[str, object]:
        try:
            self._notification_gateway_client().send_test_email()
        except (OSError, RuntimeError, ValueError) as error:
            return self._result("UNAVAILABLE", str(error))
        return self._result("READY", "Проверочное письмо отправлено.")

    def close(self) -> None:
        with self._form_review_guard:
            commands = self._form_review_commands
            thread = self._form_review_thread
            self._form_review_commands = None
            self._form_review_thread = None
        if commands is not None:
            commands.put(None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        self._journal.record(
            "desktop",
            "bridge.lifecycle",
            status="completed",
            action="close",
            account_id=self._account_id,
        )

    def _connect_telegram_notifications(self) -> dict[str, object]:
        try:
            client = self._notification_gateway_client()
            pairing = client.create_pairing_link()
            try:
                opened = webbrowser.open(pairing.start_url, new=2)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                return self._result(
                    "UNAVAILABLE",
                    "Не удалось открыть Telegram. Откройте бота вручную и повторите.",
                )
            client.wait_until_paired(
                timeout_seconds=self._settings.notification_gateway_connection_timeout_seconds,
            )
        except NotificationGatewayTimeout as error:
            return self._result("TIMEOUT", str(error))
        except (OSError, RuntimeError, ValueError) as error:
            return self._result("UNAVAILABLE", str(error))
        return self._result(
            "READY",
            "Telegram подключён к службе уведомлений.",
            service_available=True,
            key_configured=True,
            telegram=True,
            paired=True,
            telegram_bot_username=self._settings.telegram_bot_username,
        )

    def _notification_gateway_client(self) -> NotificationGatewayClient:
        credentials = load_notification_gateway_credentials(
            self._settings.notification_gateway_key_file,
        )
        if credentials is None:
            raise RuntimeError("Не настроен ключ связи со службой уведомлений")
        return NotificationGatewayClient(
            self._settings.notification_gateway_url,
            credentials,
            timeout_seconds=self._settings.notification_gateway_timeout_seconds,
        )

    def _open_form(self, vacancy_id: str) -> dict[str, object]:
        if not vacancy_id or len(vacancy_id) > 64:
            return self._result("UNAVAILABLE", "Некорректный номер вакансии")
        upgrade_database(self._settings)
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                draft = ScreeningDraftService(session).get_pending(
                    self._account_id,
                    vacancy_id,
                )
        except (LookupError, RuntimeError) as error:
            return self._result("UNAVAILABLE", str(error))
        finally:
            database.close()

        return self._review_form(draft)

    def _review_form(self, draft: ScreeningDraft) -> dict[str, object]:
        response: Queue[_FormReviewOutcome] = Queue(maxsize=1)
        command = _FormReviewCommand(draft, response)
        with self._form_review_guard:
            thread = self._form_review_thread
            commands = self._form_review_commands
            if thread is None or not thread.is_alive() or commands is None:
                commands = Queue()
                thread = threading.Thread(
                    target=self._run_form_review_browser,
                    args=(commands,),
                    name="hugin-form-review",
                    daemon=True,
                )
                self._form_review_commands = commands
                self._form_review_thread = thread
                commands.put(command)
                thread.start()
            else:
                commands.put(command)
        outcome = response.get()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _run_form_review_browser(
        self,
        commands: Queue[_FormReviewCommand | None],
    ) -> None:
        current_thread = threading.current_thread()
        try:
            with (
                self._lock,
                VisibleHhBrowser(
                    self._settings.browser_profile_dir(self._account_id),
                    self._settings.hh_login_url,
                    self._settings.hh_resumes_url,
                    self._settings.hh_search_url,
                    self._settings.hh_browser_timeout_ms,
                    browser_source_ip=(
                        str(self._settings.hh_browser_source_ip)
                        if self._settings.hh_browser_source_ip is not None
                        else None
                    ),
                ) as browser,
            ):
                while browser.is_open():
                    try:
                        command = commands.get(timeout=0.25)
                    except Empty:
                        continue
                    if command is None:
                        return
                    if not browser.is_open():
                        command.response.put(
                            self._result(
                                "UNAVAILABLE",
                                "Окно hh.ru закрыто. Нажмите «Проверить ответы» ещё раз.",
                            )
                        )
                        return
                    try:
                        result = self._prepare_form_review(command.draft, browser)
                    except Exception as error:
                        command.response.put(error)
                        return
                    command.response.put(result)
        except Exception as error:
            self._fail_pending_form_reviews(commands, error)
        finally:
            self._fail_pending_form_reviews(
                commands,
                RuntimeError("Окно hh.ru закрыто. Нажмите «Проверить ответы» ещё раз."),
            )
            with self._form_review_guard:
                if self._form_review_thread is current_thread:
                    self._form_review_commands = None
                    self._form_review_thread = None

    @staticmethod
    def _fail_pending_form_reviews(
        commands: Queue[_FormReviewCommand | None],
        error: Exception,
    ) -> None:
        while True:
            try:
                command = commands.get_nowait()
            except Empty:
                return
            if command is not None:
                command.response.put(error)

    def _prepare_form_review(
        self,
        draft: ScreeningDraft,
        browser: VisibleHhBrowser,
    ) -> dict[str, object]:
        login = HhLoginService(WindowsCredentialStore()).authenticate(
            self._account_id,
            browser,
        )
        if not login.authenticated:
            messages = {
                LoginStatus.CREDENTIALS_REQUIRED: "Сначала сохраните данные входа hh.ru",
                LoginStatus.CONFIRMATION_REQUIRED: "Введите код в открытом окне браузера",
                LoginStatus.CAPTCHA_REQUIRED: "Пройдите проверку в открытом окне браузера",
                LoginStatus.ACCOUNT_WARNING: "hh.ru показал предупреждение безопасности аккаунта",
                LoginStatus.INVALID_CREDENTIALS: "hh.ru отклонил логин или пароль",
                LoginStatus.MANUAL_ACTION_REQUIRED: "Завершите вход в открытом окне браузера",
            }
            return self._result(login.status.value.upper(), messages[login.status])

        review = browser.open_screening_form(
            draft.source_url,
            expected_resume_hh_id=draft.resume_hh_id,
            expected_resume_title=draft.resume_title,
            expected_version_hash=draft.version_hash,
            answers=draft.answers,
            cover_letter=draft.cover_letter or "",
        )
        if review.status is HhFormReviewStatus.FORM_CHANGED and review.current_form is not None:
            database = create_database(self._settings)
            try:
                with database.sessions.begin() as session:
                    ScreeningDraftService(session).capture(
                        draft.application_id,
                        review.current_form,
                    )
            finally:
                database.close()
            return self._result(
                review.status.value,
                "Работодатель изменил вопросы. Черновик обновлён; откройте его ещё раз.",
            )
        if review.status in {
            HhFormReviewStatus.VACANCY_CLOSED,
            HhFormReviewStatus.ALREADY_APPLIED,
        }:
            database = create_database(self._settings)
            try:
                with database.sessions.begin() as session:
                    ScreeningDraftService(session).invalidate(draft.form_id)
            finally:
                database.close()
        if review.status is not HhFormReviewStatus.READY:
            return self._result(
                review.status.value,
                review.message or "Анкету не удалось подготовить",
            )
        return {
            **self._result("READY", "Анкета заполнена и оставлена открытой без отправки"),
            "filled": len(review.filled_keys),
            "skipped": len(review.skipped_keys),
        }

    def _record_action(
        self,
        event: str,
        action: Callable[[], dict[str, object]],
        **details: object,
    ) -> dict[str, object]:
        run = self._journal.start(
            "desktop",
            event,
            account_id=self._account_id,
            action_details=details,
        )
        try:
            result = action()
        except Exception as error:
            run.fail(error)
            raise
        status = str(result.get("status", "UNKNOWN"))
        result_details = {
            "result_status": status,
            "result_message": result.get("message"),
        }
        if status in {"READY", "SENT"}:
            run.succeed(**result_details)
        else:
            run.block(**result_details)
        return result

    @staticmethod
    def _result(
        status: str,
        message: str,
        **details: object,
    ) -> dict[str, object]:
        return {"status": status, "message": message, **details}


def project_directory(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "compose.yaml").is_file():
            return candidate
    raise RuntimeError("Не найден compose.yaml. Запустите Hugin из каталога проекта.")


def api_is_ready(url: str) -> bool:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"{url.rstrip('/')}/health", timeout=2) as response:
            return int(response.status) == 200
    except (OSError, URLError):
        return False


def ensure_services(settings: Settings, *, timeout_seconds: int = 90) -> None:
    if api_is_ready(settings.desktop_api_url):
        return
    root = project_directory()
    database = subprocess.run(
        ["docker", "compose", "up", "--detach", "--wait", "db"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    if database.returncode != 0:
        raise RuntimeError("Не удалось запустить PostgreSQL для резервного копирования")
    BackupService(
        settings,
        adapter=DockerPostgresBackupAdapter(root),
    ).create("pre-update")
    completed = subprocess.run(
        ["docker", "compose", "up", "--detach", "--build", "--wait"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    if completed.returncode != 0:
        raise RuntimeError("Не удалось запустить PostgreSQL и сервер Hugin")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if api_is_ready(settings.desktop_api_url):
            return
        time.sleep(1)
    raise RuntimeError("Сервер Hugin не стал доступен вовремя")


@contextmanager
def single_desktop_instance(port: int = 47631) -> Iterator[None]:
    instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        instance_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_EXCLUSIVEADDRUSE,
            1,
        )
    try:
        instance_socket.bind(("127.0.0.1", port))
    except OSError as error:
        instance_socket.close()
        raise RuntimeError(
            "Hugin уже запущен. Откройте существующее окно на панели задач."
        ) from error
    try:
        yield
    finally:
        instance_socket.close()


def show_launch_error(message: str) -> None:
    title = "Hugin — ошибка запуска"
    if os.name != "nt":
        print(f"{title}: {message}")
        return
    import ctypes

    windows = cast(WindowsLibraries, vars(ctypes)["windll"])
    windows.user32.MessageBoxW(None, message, title, 0x10)


def main() -> None:
    settings = get_settings()
    journal = OperationJournal(settings.data_dir)
    starting = journal.start(
        "desktop",
        "application.start",
        environment=settings.environment,
    )
    try:
        ensure_services(settings)
        webview = cast(WebviewModule, import_module("webview"))
    except ImportError as error:
        failure = RuntimeError("Установите оконную часть: uv sync --extra desktop --extra browser")
        starting.fail(failure)
        raise failure from error
    except Exception as error:
        starting.fail(error)
        raise
    browser_lock = threading.Lock()
    search_handler = HhSearchJobHandler(
        settings,
        browser_lock=browser_lock,
    )
    messages_handler = HhSyncJobHandler(
        settings,
        AutomationJobKind.MESSAGES,
        browser_lock=browser_lock,
    )
    statuses_handler = HhSyncJobHandler(
        settings,
        AutomationJobKind.STATUSES,
        browser_lock=browser_lock,
    )
    worker = AutomationWorker(
        settings,
        handlers={
            HhSearchJobHandler.kind: search_handler,
            AutomationJobKind.MESSAGES: messages_handler,
            AutomationJobKind.STATUSES: statuses_handler,
        },
        authentication_recovery=messages_handler.recover_authentication,
        journal=journal,
    )
    application_worker = ApplicationWorker(
        settings,
        browser_lock=browser_lock,
        journal=journal,
    )
    notification_worker = NotificationWorker(settings, journal=journal)
    backup_worker = BackupWorker(settings, journal=journal)
    bridge = DesktopBridge(
        settings,
        browser_lock=browser_lock,
        journal=journal,
    )
    workers: tuple[BackgroundWorker, ...] = (
        worker,
        application_worker,
        notification_worker,
        backup_worker,
    )
    started_workers: list[BackgroundWorker] = []
    try:
        for background_worker in workers:
            background_worker.start()
            started_workers.append(background_worker)
        webview.create_window(
            "Hugin — поиск работы",
            settings.desktop_api_url,
            js_api=bridge,
            width=1440,
            height=920,
            min_size=(1080, 700),
            background_color="#f4f7f5",
        )
    except Exception as error:
        starting.fail(error)
        bridge.close()
        for background_worker in reversed(started_workers):
            background_worker.stop()
        raise
    starting.succeed(workers=len(started_workers))
    session = journal.start("desktop", "application.session")
    try:
        webview.start(debug=False, icon=str(APP_ICON))
    except Exception as error:
        session.fail(error)
        raise
    else:
        session.succeed()
    finally:
        bridge.close()
        for background_worker in reversed(started_workers):
            background_worker.stop()


def launch() -> None:
    try:
        with single_desktop_instance():
            main()
    except Exception as error:
        with suppress(Exception):
            settings = get_settings()
            OperationJournal(settings.data_dir).record(
                "desktop",
                "application.launch",
                status="failed",
                level="ERROR",
                **error_details(error),
            )
        show_launch_error(str(error))


if __name__ == "__main__":
    launch()
