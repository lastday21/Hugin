from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from hugin.database.engine import Database
from hugin.diagnostics import OperationJournal, error_details


@dataclass(frozen=True, slots=True)
class _Job:
    id: str
    key: tuple[str, int]
    future: Future[bytes]


class WorkspaceReads:
    """Keep expensive reads outside HTTP requests and share only overlapping work."""

    def __init__(self, database: Database, journal: OperationJournal) -> None:
        self._database = database
        self._journal = journal
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hugin-reads")
        self._lock = Lock()
        self._pending: dict[tuple[str, int], _Job] = {}
        self._jobs: dict[str, _Job] = {}
        self._completed: deque[str] = deque()

    def start(self, kind: str, account_id: int, load: Callable[[Session, int], object]) -> Response:
        key = (kind, account_id)
        with self._lock:
            job = self._pending.get(key)
            created = job is None or job.future.done()
            if created:
                job_id = uuid4().hex
                job = _Job(job_id, key, self._executor.submit(self._run, key, load, job_id))
                self._pending[key] = job
                self._jobs[job.id] = job
        assert job is not None
        if created:
            # A callback may run immediately for an already completed future.
            job.future.add_done_callback(lambda _future: self._finish(job))
        return self._response(job)

    def get(self, account_id: int, job_id: str) -> Response:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.key[1] != account_id:
            raise HTTPException(status_code=404, detail="Загрузка данных не найдена")
        return self._response(job)

    def invalidate(self) -> None:
        # Existing pollers retain their jobs; reads after a commit start afresh.
        with self._lock:
            self._pending.clear()

    def _run(
        self, key: tuple[str, int], load: Callable[[Session, int], object], job_id: str
    ) -> bytes:
        # Like read_session, this session is closed without committing.
        try:
            with self._database.sessions() as session:
                result = load(session, key[1])
                return bytes(JSONResponse(content=jsonable_encoder(result)).body)
        except Exception as error:
            details = {
                "job_id": job_id,
                "kind": key[0],
                "account_id": key[1],
                **error_details(error),
            }
            saved = self._journal.save_evidence(job_id, "failure", details)
            self._journal.record(
                "server",
                "workspace.read",
                status="failed",
                level="ERROR",
                run_id=job_id,
                failure_evidence_saved=saved,
                **details,
            )
            raise

    def _finish(self, job: _Job) -> None:
        with self._lock:
            if self._pending.get(job.key) is job:
                del self._pending[job.key]
            self._completed.append(job.id)
            while len(self._completed) > 64:
                del self._jobs[self._completed.popleft()]

    @staticmethod
    def _response(job: _Job) -> Response:
        if not job.future.done():
            return JSONResponse(
                status_code=202,
                content={"kind": "workspace-read", "job_id": job.id, "state": "running"},
            )
        try:
            body = job.future.result()
        except Exception as error:
            raise HTTPException(
                status_code=500, detail="Ошибка загрузки данных. Повторите обновление."
            ) from error
        return Response(content=body, media_type="application/json")

    def close(self) -> None:
        self._executor.shutdown(wait=True)
