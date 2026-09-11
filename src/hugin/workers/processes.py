from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import BackgroundProcessRunModel, HhAccountModel
from hugin.diagnostics import OperationJournal, operation_context
from hugin.domain.time import as_utc
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.background_processes import PROCESS_KEYS, BackgroundProcessService, ProcessKey

type ProcessStep = Callable[[int | None], bool]


class BackgroundProcessWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        steps: Mapping[ProcessKey, ProcessStep],
        account_id: int = 1,
        poll_seconds: float = 2,
        heartbeat_seconds: float = 5,
        journal: OperationJournal | None = None,
        cancel: Callable[[], None] | None = None,
    ) -> None:
        if account_id < 1 or poll_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("Аккаунт и интервалы должны быть положительными")
        self._settings = settings
        self._account_id = account_id
        self._steps = dict(steps)
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._journal = journal or OperationJournal(settings.data_dir)
        self._cancel = cancel
        self._position = 0
        self._initialized = False
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
            name="hugin-background-processes",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 10) -> None:
        self._stop.set()
        if self._cancel is not None:
            self._cancel()
        if self._thread is not None:
            self._thread.join(timeout_seconds)
            if not self._thread.is_alive():
                self._thread = None

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        database = create_database(self._settings)
        try:
            # Отдельное соединение удерживает блокировку до завершения всего хода.
            with database.engine.connect() as ownership:
                if not ownership.scalar(
                    text("SELECT pg_try_advisory_lock(684721, :account)"),
                    {"account": self._account_id},
                ):
                    return False
                try:
                    with database.sessions.begin() as session:
                        interrupted = self._recover_processes(session)
                    if interrupted:
                        self._journal.record(
                            "processes",
                            "recovered",
                            status="completed",
                            account_id=self._account_id,
                            interrupted_processes=interrupted,
                            next_process=PROCESS_KEYS[self._position],
                        )
                    if not self._initialized:
                        with database.sessions.begin() as session:
                            ApplicationAutomationService(session).recover_interrupted()
                        self._initialized = True
                    for _ in PROCESS_KEYS:
                        key = PROCESS_KEYS[self._position]
                        self._position = (self._position + 1) % len(PROCESS_KEYS)
                        if key not in self._steps or self._stop.is_set():
                            continue
                        with database.sessions.begin() as session:
                            if session.get(HhAccountModel, self._account_id) is None:
                                return False
                            service = BackgroundProcessService(session, self._account_id)
                            token = (
                                service.claim_check_now_token()
                                if key == "synchronization"
                                else None
                            )
                            if token is None and not service.enabled(key):
                                continue
                            service.started(key)
                        self._execute(key, token)
                        return True
                    return False
                finally:
                    ownership.execute(
                        text("SELECT pg_advisory_unlock(684721, :account)"),
                        {"account": self._account_id},
                    )
        finally:
            database.close()

    def _recover_processes(self, session: Session) -> list[str]:
        rows = list(
            session.scalars(
                select(BackgroundProcessRunModel)
                .where(BackgroundProcessRunModel.account_id == self._account_id)
                .with_for_update()
            )
        )
        started = [
            (as_utc(row.last_started_at), row.key)
            for row in rows
            if row.key in PROCESS_KEYS and row.last_started_at is not None
        ]
        if started:
            _, latest_key = max(started)
            self._position = (PROCESS_KEYS.index(latest_key) + 1) % len(PROCESS_KEYS)
        interrupted = []
        for row in rows:
            if row.state == "running":
                row.state = "interrupted"
                row.reason = "Предыдущий ход прерван; продолжение сохранено"
                interrupted.append(row.key)
        return interrupted

    def _execute(self, key: ProcessKey, token: int | None) -> None:
        finished = threading.Event()
        pulse = threading.Thread(target=self._heartbeat, args=(key, finished), daemon=True)
        pulse.start()
        database = create_database(self._settings)
        run = self._journal.start("processes", key, account_id=self._account_id)
        try:
            with operation_context(parent_run_id=run.run_id):
                worked = self._steps[key](token)
        except Exception as error:
            with database.sessions.begin() as session:
                BackgroundProcessService(session, self._account_id).failed(key, str(error))
            run.fail(error)
        else:
            with database.sessions.begin() as session:
                BackgroundProcessService(session, self._account_id).finished(key, worked)
            run.succeed(worked=worked)
        finally:
            finished.set()
            pulse.join()
            database.close()

    def _heartbeat(self, key: ProcessKey, finished: threading.Event) -> None:
        database = create_database(self._settings)
        try:
            while not finished.wait(self._heartbeat_seconds):
                try:
                    with database.sessions.begin() as session:
                        BackgroundProcessService(session, self._account_id).heartbeat(key)
                except Exception as error:
                    self._journal.record(
                        "processes", "heartbeat", status="failed", error=str(error)
                    )
        finally:
            database.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:
                self._journal.record("processes", "worker", status="failed", error=str(error))
            self._stop.wait(self._poll_seconds)
