from __future__ import annotations

import threading
from dataclasses import dataclass, field

from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal


@dataclass
class _BrowserOwner:
    ready: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    endpoint: str | None = None
    error: Exception | None = None


class SharedHhBrowser:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        journal: OperationJournal | None = None,
    ) -> None:
        self._settings = settings
        self._account_id = account_id
        self._journal = journal or OperationJournal(settings.data_dir)
        self._guard = threading.Lock()
        self._owner: _BrowserOwner | None = None
        self._closed = False

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
        with self._guard:
            self._closed = True
            owner = self._owner
            if owner is not None:
                owner.stop.set()
        if owner is not None and owner.thread is not None:
            owner.thread.join(timeout_seconds)

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
                while not owner.stop.wait(1):
                    if not browser.is_open():
                        break
            run.succeed()
        except Exception as error:
            owner.error = error
            run.fail(error)
        finally:
            owner.stop.set()
            owner.ready.set()
