from __future__ import annotations

import threading
from collections.abc import Callable
from uuid import uuid4

import psycopg

from hugin.core.settings import Settings
from hugin.database import postgresql_url
from hugin.diagnostics import OperationJournal, error_details
from hugin.repositories.tasks import CAPTCHA_AGENT_CHANNEL
from hugin.services.captcha_agent import CaptchaAgentDispatcher


class CaptchaAgentWorker:
    """Block on PostgreSQL notifications; never poll the agent or application state."""

    def __init__(
        self,
        settings: Settings,
        *,
        journal: OperationJournal | None = None,
        dispatch: Callable[[], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._journal = journal or OperationJournal(settings.data_dir)
        self._dispatch = (
            dispatch or CaptchaAgentDispatcher(settings, journal=self._journal).dispatch_current
        )
        self._stop = threading.Event()
        self.ready = threading.Event()
        self._stop_token = f"stop:{uuid4().hex}"
        self._thread: threading.Thread | None = None
        self._dsn = (
            postgresql_url(settings)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self._settings.captcha_agent_thread_id or self.running:
            return
        self._stop.clear()
        self.ready.clear()
        self._thread = threading.Thread(target=self._run, name="hugin-captcha-agent", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 10) -> None:
        self._stop.set()
        if self.running:
            try:
                with psycopg.connect(self._dsn, autocommit=True, connect_timeout=2) as connection:
                    connection.execute(
                        "SELECT pg_notify(%s, %s)", (CAPTCHA_AGENT_CHANNEL, self._stop_token)
                    )
            except psycopg.Error:
                pass  # A disconnected listener already leaves its wait and observes _stop.
        if self._thread is not None:
            self._thread.join(timeout_seconds)
            if not self._thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with psycopg.connect(
                    self._dsn,
                    autocommit=True,
                    connect_timeout=self._settings.database_connect_timeout,
                    application_name="hugin-captcha-agent",
                ) as connection:
                    connection.execute(f"LISTEN {CAPTCHA_AGENT_CHANNEL}")
                    # One read after connecting also catches a committed event missed while down.
                    self._dispatch()
                    self.ready.set()
                    self._journal.record("captcha", "agent.listen", status="ready")
                    for notification in connection.notifies():
                        if self._stop.is_set() or notification.payload == self._stop_token:
                            break
                        self._dispatch()
            except Exception as error:
                self.ready.set()
                if not self._stop.is_set():
                    self._journal.record(
                        "captcha",
                        "agent.listen",
                        status="failed",
                        level="ERROR",
                        **error_details(error),
                    )
                    self._stop.wait(3)  # Reconnect only after a broken connection, not as polling.
