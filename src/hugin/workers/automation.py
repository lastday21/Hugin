from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.diagnostics import OperationJournal, error_details
from hugin.domain.automation import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
    AutomationJobStateError,
)
from hugin.services.automation import AutomationSchedulerService

type AutomationJobHandler = Callable[[AutomationJobRecord], AutomationJobResult]

DEFAULT_HEARTBEAT_SECONDS = 30.0


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
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        journal: OperationJournal | None = None,
    ) -> None:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")
        if poll_seconds <= 0:
            raise ValueError("Интервал проверки расписания должен быть положительным")
        if heartbeat_seconds <= 0:
            raise ValueError("Интервал пульса фонового задания должен быть положительным")
        self._settings = settings
        self._account_id = account_id
        self._handlers = dict(handlers or {})
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._journal = journal or OperationJournal(settings.data_dir)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        starting = self._journal.start(
            "automation",
            "worker.lifecycle",
            action="start",
            account_id=self._account_id,
        )
        try:
            upgrade_database(self._settings)
        except Exception as error:
            starting.fail(error)
            raise
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hugin-background-checks",
            daemon=True,
        )
        self._thread.start()
        starting.succeed()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_seconds)
        self._thread = None
        self._journal.record(
            "automation",
            "worker.lifecycle",
            status="completed",
            action="stop",
            account_id=self._account_id,
        )

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

            run = self._journal.start(
                "automation",
                "scheduled_job",
                account_id=job.account_id,
                job_key=job.key,
                job_kind=job.kind.value,
                search_query_id=job.search_query_id,
                interval_seconds=job.interval_seconds,
                previous_failures=job.consecutive_failures,
            )
            handler = self._handlers.get(job.kind)
            if handler is None:
                with database.sessions.begin() as session:
                    AutomationSchedulerService(session).block(
                        job.key,
                        error_code="SOURCE_NOT_CONNECTED",
                        error_message=self._missing_handler_message(job.kind),
                        now=now,
                    )
                run.block(error_code="SOURCE_NOT_CONNECTED")
                return True

            try:
                result = self._run_handler(job, handler)
            except AutomationJobBlocked as error:
                with database.sessions.begin() as session:
                    AutomationSchedulerService(session).block(
                        job.key,
                        error_code=error.code,
                        error_message=str(error),
                        now=now,
                    )
                run.block(error_code=error.code, error_message=str(error))
            except Exception as error:
                with database.sessions.begin() as session:
                    AutomationSchedulerService(session).fail(
                        job.key,
                        error_code=type(error).__name__,
                        error_message=str(error) or "Фоновая проверка завершилась ошибкой",
                        now=now,
                    )
                run.fail(error)
            else:
                with database.sessions.begin() as session:
                    AutomationSchedulerService(session).complete(job.key, result, now)
                run.succeed(result=result)
            return True
        finally:
            database.close()

    def _run_handler(
        self,
        job: AutomationJobRecord,
        handler: AutomationJobHandler,
    ) -> AutomationJobResult:
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=lambda: self._heartbeat_job(job.key, heartbeat_stop),
            name=f"hugin-heartbeat-{job.key}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            return handler(job)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join()

    def _heartbeat_job(self, job_key: str, stop: threading.Event) -> None:
        if stop.wait(self._heartbeat_seconds):
            return
        try:
            database = create_database(self._settings)
        except Exception as error:
            self._record_heartbeat_error(job_key, error)
            return
        try:
            while not stop.is_set():
                try:
                    with database.sessions.begin() as session:
                        AutomationSchedulerService(session).heartbeat(job_key)
                except AutomationJobStateError:
                    return
                except Exception as error:
                    self._record_heartbeat_error(job_key, error)
                if stop.wait(self._heartbeat_seconds):
                    return
        finally:
            database.close()

    def _record_heartbeat_error(self, job_key: str, error: Exception) -> None:
        self._journal.record(
            "automation",
            "job.heartbeat",
            status="failed",
            level="WARNING",
            account_id=self._account_id,
            job_key=job_key,
            **error_details(error),
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception as error:
                self._journal.record(
                    "automation",
                    "worker.loop",
                    status="failed",
                    level="ERROR",
                    account_id=self._account_id,
                    **error_details(error),
                )
                worked = False
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
