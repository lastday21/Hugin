from __future__ import annotations

import threading
from contextlib import suppress

from sqlalchemy import select

from hugin.adapters.notifications import NotificationContent, WindowsToastSender
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationSettingsModel, IncidentModel
from hugin.diagnostics import OperationJournal
from hugin.domain.content import IncidentSeverity, IncidentState
from hugin.services.backups import BackupService


class BackupWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        poll_seconds: float = 3600,
        journal: OperationJournal | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("Интервал резервного копирования должен быть положительным")
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._journal = journal or OperationJournal(settings.data_dir)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_reported_error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hugin-backups",
            daemon=True,
        )
        self._thread.start()
        self._journal.record(
            "backups",
            "worker.lifecycle",
            status="completed",
            action="start",
        )

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_seconds)
        self._thread = None
        self._journal.record(
            "backups",
            "worker.lifecycle",
            status="completed",
            action="stop",
        )

    def run_once(self) -> bool:
        retention_days = self._retention_days()
        return BackupService(self._settings).ensure_daily(retention_days=retention_days) is not None

    def _retention_days(self) -> int:
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                value = session.scalar(
                    select(ApplicationSettingsModel.backups_retention_days).where(
                        ApplicationSettingsModel.id == 1
                    )
                )
        finally:
            database.close()
        return value or 30

    def _log_retention_days(self) -> int:
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                value = session.scalar(
                    select(ApplicationSettingsModel.logs_retention_days).where(
                        ApplicationSettingsModel.id == 1
                    )
                )
        finally:
            database.close()
        return value or 90

    def _record_failure(self, message: str) -> None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                incident = session.scalar(
                    select(IncidentModel).where(
                        IncidentModel.code == "BACKUP_FAILED",
                        IncidentModel.state == IncidentState.OPEN,
                    )
                )
                if incident is None:
                    session.add(
                        IncidentModel(
                            code="BACKUP_FAILED",
                            severity=IncidentSeverity.ERROR,
                            state=IncidentState.OPEN,
                            scope_type="backup",
                            message=message[:500],
                        )
                    )
                else:
                    incident.message = message[:500]
        finally:
            database.close()

    def _resolve_failure(self) -> None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                incidents = session.scalars(
                    select(IncidentModel).where(
                        IncidentModel.code == "BACKUP_FAILED",
                        IncidentModel.state == IncidentState.OPEN,
                    )
                )
                for incident in incidents:
                    incident.state = IncidentState.RESOLVED
        finally:
            database.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            run = self._journal.start("backups", "daily_backup")
            try:
                created = self.run_once()
            except Exception as error:
                message = f"Не удалось создать резервную копию: {type(error).__name__}"
                run.fail(error)
                with suppress(Exception):
                    self._record_failure(message)
                if message != self._last_reported_error:
                    with suppress(Exception):
                        WindowsToastSender().send(
                            NotificationContent("Hugin требует внимания", message)
                        )
                    self._last_reported_error = message
            else:
                if created:
                    run.succeed()
                else:
                    run.skip(reason="fresh_backup_exists")
                self._last_reported_error = None
                with suppress(Exception):
                    self._resolve_failure()
            with suppress(Exception):
                removed = self._journal.prune(self._log_retention_days())
                if removed:
                    self._journal.record(
                        "backups",
                        "journal.retention",
                        status="completed",
                        removed_files=removed,
                    )
            self._stop.wait(self._poll_seconds)
