from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain.automation import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
)
from hugin.services.automation import AutomationSchedulerService

type AutomationJobHandler = Callable[[AutomationJobRecord], AutomationJobResult]


class AutomationJobBlocked(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code.strip()[:64] or "AUTOMATION_BLOCKED"


class AutomationWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        handlers: Mapping[AutomationJobKind, AutomationJobHandler] | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")
        if poll_seconds <= 0:
            raise ValueError("Интервал проверки расписания должен быть положительным")
        self._settings = settings
        self._account_id = account_id
        self._handlers = dict(handlers or {})
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        upgrade_database(self._settings)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hugin-background-checks",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_seconds)
        self._thread = None

    def run_once(self, now: datetime | None = None) -> bool:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                scheduler = AutomationSchedulerService(session)
                configured = scheduler.ensure_configured_jobs(self._account_id, now)
                for configured_job in configured:
                    if (
                        configured_job.state is AutomationJobState.BLOCKED
                        and configured_job.last_error_code == "SOURCE_NOT_CONNECTED"
                        and configured_job.kind in self._handlers
                    ):
                        scheduler.unblock(configured_job.key, now)
                scheduler.recover_stale(now)
            with database.sessions.begin() as session:
                job = AutomationSchedulerService(session).claim_due(now)
            if job is None:
                return False

            handler = self._handlers.get(job.kind)
            if handler is None:
                with database.sessions.begin() as session:
                    AutomationSchedulerService(session).block(
                        job.key,
                        error_code="SOURCE_NOT_CONNECTED",
                        error_message=self._missing_handler_message(job.kind),
                        now=now,
                    )
                return True

            try:
                result = handler(job)
            except AutomationJobBlocked as error:
                with database.sessions.begin() as session:
                    AutomationSchedulerService(session).block(
                        job.key,
                        error_code=error.code,
                        error_message=str(error),
                        now=now,
                    )
            except Exception as error:
                with database.sessions.begin() as session:
                    AutomationSchedulerService(session).fail(
                        job.key,
                        error_code=type(error).__name__,
                        error_message=str(error) or "Фоновая проверка завершилась ошибкой",
                        now=now,
                    )
            else:
                with database.sessions.begin() as session:
                    AutomationSchedulerService(session).complete(job.key, result, now)
            return True
        finally:
            database.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            worked = self.run_once()
            if not worked:
                self._stop.wait(self._poll_seconds)

    @staticmethod
    def _missing_handler_message(kind: AutomationJobKind) -> str:
        names = {
            AutomationJobKind.SEARCH: "Фоновый поиск пока не подключён к окну hh.ru",
            AutomationJobKind.MESSAGES: "Фоновая проверка сообщений пока не подключена к hh.ru",
            AutomationJobKind.STATUSES: "Фоновая проверка статусов пока не подключена к hh.ru",
        }
        return names[kind]
