from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database, upgrade_database
from hugin.domain.hh import HhFormReviewStatus
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.screening_forms import ScreeningDraftService
from hugin.workers.automation import AutomationWorker
from hugin.workers.hh_search import HhSearchJobHandler


class WebviewWindow(Protocol):
    def destroy(self) -> None: ...


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

    def start(self, *, debug: bool = False) -> None: ...


class DesktopBridge:
    def __init__(
        self,
        settings: Settings,
        account_id: int = 1,
        *,
        browser_lock: threading.Lock | None = None,
    ) -> None:
        self._settings = settings
        self._account_id = account_id
        self._lock = browser_lock or threading.Lock()

    def open_form(self, vacancy_id: str) -> dict[str, object]:
        with self._lock:
            return self._open_form(vacancy_id.strip())

    def open_url(self, url: str) -> dict[str, object]:
        value = url.strip()
        if not value.startswith(("https://hh.ru/", "https://www.hh.ru/")):
            return self._result("UNAVAILABLE", "Разрешены только ссылки hh.ru")
        opened = webbrowser.open(value, new=2)
        return self._result("READY" if opened else "UNAVAILABLE", "Ссылка открыта")

    def open_invitation(self, invitation_id: int) -> dict[str, object]:
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

    def close(self) -> None:
        return None

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

        with VisibleHhBrowser(
            self._settings.browser_profile_dir(self._account_id),
            self._settings.hh_login_url,
            self._settings.hh_resumes_url,
            self._settings.hh_search_url,
            self._settings.hh_browser_timeout_ms,
        ) as browser:
            login = HhLoginService(WindowsCredentialStore()).authenticate(
                self._account_id,
                browser,
            )
            if not login.authenticated:
                messages = {
                    LoginStatus.CREDENTIALS_REQUIRED: "Сначала сохраните данные входа hh.ru",
                    LoginStatus.CONFIRMATION_REQUIRED: "Введите код в открытом окне браузера",
                    LoginStatus.CAPTCHA_REQUIRED: "Пройдите проверку в открытом окне браузера",
                    LoginStatus.INVALID_CREDENTIALS: "hh.ru отклонил логин или пароль",
                    LoginStatus.MANUAL_ACTION_REQUIRED: "Завершите вход в открытом окне браузера",
                }
                return self._result(login.status.value.upper(), messages[login.status])

            review = browser.open_screening_form(
                draft.source_url,
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

    @staticmethod
    def _result(status: str, message: str) -> dict[str, object]:
        return {"status": status, "message": message}


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

    user32 = ctypes.windll.user32
    user32.MessageBoxW(None, message, title, 0x10)


def main() -> None:
    settings = get_settings()
    ensure_services(settings)
    try:
        webview = cast(WebviewModule, import_module("webview"))
    except ImportError as error:
        raise RuntimeError(
            "Установите оконную часть: uv sync --extra desktop --extra browser"
        ) from error
    browser_lock = threading.Lock()
    worker = AutomationWorker(
        settings,
        handlers={
            HhSearchJobHandler.kind: HhSearchJobHandler(
                settings,
                browser_lock=browser_lock,
            )
        },
    )
    bridge = DesktopBridge(settings, browser_lock=browser_lock)
    worker.start()
    try:
        webview.create_window(
            "Hugin — поиск работы",
            settings.desktop_api_url,
            js_api=bridge,
            width=1440,
            height=920,
            min_size=(1080, 700),
            background_color="#f4f7f5",
        )
        webview.start(debug=False)
    finally:
        bridge.close()
        worker.stop()


def launch() -> None:
    try:
        with single_desktop_instance():
            main()
    except Exception as error:
        show_launch_error(str(error))


if __name__ == "__main__":
    launch()
