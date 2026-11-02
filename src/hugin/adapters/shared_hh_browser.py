from __future__ import annotations

import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from time import monotonic, sleep
from types import TracebackType

from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.browser_control import BrowserControl
from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal


@dataclass
class _BrowserOwner:
    ready: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    endpoint: str | None = None
    error: Exception | None = None
    headless: bool = True


class SharedHhBrowser:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        journal: OperationJournal | None = None,
        browser_locks: tuple[threading.Lock, ...] = (),
    ) -> None:
        self._settings = settings
        self._account_id = account_id
        self._journal = journal or OperationJournal(settings.data_dir)
        self._guard = threading.Lock()
        self._owner: _BrowserOwner | None = None
        self._closed = False
        self._browser_locks = browser_locks
        self.control = BrowserControl(settings.data_dir, account_id)
        self._monitor_stop = threading.Event()
        self._monitor: threading.Thread | None = None

    def start(self) -> None:
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._monitor = threading.Thread(target=self._watch_control, daemon=True)
        self._monitor.start()

    def automatic_allowed(self) -> bool:
        return not self.control.active()

    def manual_lock(self) -> _ManualBrowserLock:
        return _ManualBrowserLock(self)

    def _watch_control(self) -> None:
        while not self._monitor_stop.wait(0.5):
            request: dict[str, object] = {}
            try:
                request = self.control.read()
                if request.get("state") == "requested" and self.control.active():
                    self.endpoint()
            except Exception as error:
                self._journal.record("browser", "manual_control", status="failed", error=str(error))
                if request.get("token"):
                    self.control.mark_ready(str(request["token"]), error=str(error))
                self._monitor_stop.wait(5)

    def endpoint(self) -> str:
        with self._guard:
            if self._closed:
                raise RuntimeError("Общий браузер остановлен")
            owner = self._owner
            if owner is None or owner.thread is None or not owner.thread.is_alive():
                owner = _BrowserOwner()
                owner.thread = threading.Thread(
                    target=self._serve, args=(owner,), name="hugin-browser-owner", daemon=True
                )
                self._owner = owner
                owner.thread.start()
            elif not self.control.active() and not owner.headless:
                owner.ready.clear()
        if not owner.ready.wait(self._settings.hh_browser_timeout_ms / 1000 + 10):
            raise RuntimeError("Истекло время ожидания запуска общего браузера")
        if owner.error is not None:
            if owner.thread is not None:
                owner.thread.join(1)
            raise RuntimeError(str(owner.error)) from owner.error
        if owner.endpoint is None or owner.stop.is_set():
            raise RuntimeError("Общий браузер закрыт")
        return owner.endpoint

    def stop(self, timeout_seconds: float = 10) -> None:
        self._monitor_stop.set()
        with self._guard:
            self._closed = True
            owner = self._owner
            if owner is not None:
                owner.stop.set()
        if owner is not None and owner.thread is not None:
            owner.thread.join(timeout_seconds)
        if self._monitor is not None:
            self._monitor.join(timeout_seconds)

    def _serve(self, owner: _BrowserOwner) -> None:
        run = self._journal.start("browser", "shared_session", account_id=self._account_id)
        try:
            with VisibleHhBrowser(
                self._settings.browser_profile_dir(self._account_id),
                self._settings.hh_login_url,
                self._settings.hh_resumes_url,
                self._settings.hh_search_url,
                self._settings.hh_browser_timeout_ms,
                start_minimized=True,
                headless=True,
                browser_channel=self._settings.hh_browser_channel,
                browser_source_ip=(
                    str(self._settings.hh_browser_source_ip)
                    if self._settings.hh_browser_source_ip is not None
                    else None
                ),
                remote_debugging=True,
                journal=self._journal,
            ) as browser:
                owner.endpoint = browser.debugging_endpoint()
                owner.ready.set()
                with ExitStack() as held:
                    current_token: str | None = None
                    while not owner.stop.wait(0.1):
                        if not browser.is_open():
                            break
                        current_token = self._manual_tick(browser, owner, held, current_token)
            run.succeed()
        except Exception as error:
            owner.error = error
            run.fail(error)
        finally:
            owner.stop.set()
            owner.ready.set()

    def _manual_tick(
        self,
        browser: VisibleHhBrowser,
        owner: _BrowserOwner,
        held: ExitStack,
        current_token: str | None,
    ) -> str | None:
        request = self.control.read_active()
        if not request:
            if current_token is not None or not owner.headless:
                owner.ready.clear()
                browser.return_to_background()
                self._journal.record("browser", "manual_control", status="released")
                owner.endpoint = browser.debugging_endpoint()
                owner.headless = True
                owner.ready.set()
            held.close()
            return None
        token = str(request["token"])
        if token == current_token:
            return current_token
        held.close()
        if request.get("state") == "failed":
            return None
        for lock in self._browser_locks:
            while not lock.acquire(timeout=0.1):
                if owner.stop.is_set() or self.control.read_active().get("token") != token:
                    held.close()
                    return None
            held.callback(lock.release)
        if owner.stop.is_set() or self.control.read_active().get("token") != token:
            held.close()
            return None
        try:
            if request.get("show_window", True):
                browser.ensure_visible()
                owner.endpoint = browser.debugging_endpoint()
                owner.headless = False
            if request.get("url"):
                browser.show_verification(request["url"])
            if not self.control.mark_ready(token):
                held.close()
                return None
        except Exception as error:
            held.close()
            self.control.mark_ready(token, error=str(error))
            return None
        self._journal.record("browser", "manual_control", status="ready")
        return token


class _ManualBrowserLock:
    def __init__(self, browser: SharedHhBrowser) -> None:
        self._browser = browser
        self._guard = threading.Lock()
        self._token: str | None = None

    def __enter__(self) -> None:
        self._guard.acquire()
        try:
            self._browser.endpoint()
            request = self._browser.control.acquire(None, minutes=60)
            self._token = str(request["token"])
            deadline = monotonic() + self._browser._settings.hh_browser_timeout_ms / 1000 + 10
            while monotonic() < deadline:
                state = self._browser.control.read_active()
                if state.get("token") != self._token:
                    raise RuntimeError("Передача управления браузером отменена")
                if state.get("state") == "ready":
                    return
                if state.get("state") == "failed":
                    raise RuntimeError(state.get("error", "Браузер недоступен"))
                sleep(0.1)
            raise RuntimeError("Текущие действия браузера не завершились вовремя")
        except Exception:
            try:
                if self._token is not None:
                    self._browser.control.release(self._token, strict=False)
            finally:
                self._token = None
                self._guard.release()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._token is not None:
                self._browser.control.release(self._token, strict=False)
                self._token = None
        finally:
            self._guard.release()
