from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Literal
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

from hugin.database.engine import Database
from hugin.diagnostics import OperationJournal


@dataclass(frozen=True, slots=True)
class FormReconciliationStatus:
    job_id: str
    state: Literal["pending", "running", "completed", "failed"]
    reconciled: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Job:
    id: str
    account_id: int
    future: Future[int]


class FormReconciliationJobs:
    def __init__(
        self,
        database: Database,
        journal: OperationJournal,
        *,
        after_commit: Callable[[], None] | None = None,
    ) -> None:
        self._database = database
        self._journal = journal
        self._after_commit = after_commit
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hugin-forms")
        self._lock = Lock()
        self._jobs: dict[str, _Job] = {}
        self._accounts: dict[int, _Job] = {}

    def start(
        self, account_id: int, action: Callable[[Session, int], int]
    ) -> FormReconciliationStatus:
        with self._lock:
            current = self._accounts.get(account_id)
            if current is not None and not current.future.done():
                return self._status(current)
            job_id = uuid4().hex
            job = _Job(
                job_id, account_id, self._executor.submit(self._run, account_id, action, job_id)
            )
            self._jobs[job.id] = job
            self._accounts[account_id] = job
            # Keep recent completions for clients still polling an earlier job.
            for job_id, previous in tuple(self._jobs.items()):
                if len(self._jobs) <= 64:
                    break
                if previous.future.done():
                    del self._jobs[job_id]
            return self._status(job)

    def get(self, account_id: int, job_id: str) -> FormReconciliationStatus:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.account_id != account_id:
                raise LookupError("Проверка анкет не найдена")
            return self._status(job)

    def _run(self, account_id: int, action: Callable[[Session, int], int], job_id: str) -> int:
        run = self._journal.start("server", "forms.reconcile", account_id=account_id, job_id=job_id)
        changed = False

        def flushed(_session: Session, _context: object) -> None:
            nonlocal changed
            changed = True

        try:
            with self._database.sessions.begin() as session:
                event.listen(session, "after_flush", flushed)
                reconciled = action(session, account_id)
            if changed and self._after_commit is not None:
                self._after_commit()
        except Exception as error:
            run.fail(error)
            raise
        run.succeed(reconciled=reconciled)
        return reconciled

    @staticmethod
    def _status(job: _Job) -> FormReconciliationStatus:
        if not job.future.done():
            return FormReconciliationStatus(
                job.id, "running" if job.future.running() else "pending"
            )
        if job.future.exception() is not None:
            return FormReconciliationStatus(
                job.id,
                "failed",
                error="Проверить анкеты не удалось. Проверка повторится автоматически.",
            )
        return FormReconciliationStatus(job.id, "completed", reconciled=job.future.result())

    def close(self) -> None:
        self._executor.shutdown(wait=True)
