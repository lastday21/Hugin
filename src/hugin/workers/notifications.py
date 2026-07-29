# ruff: noqa: RUF001

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from hugin.adapters.notification_credentials import WindowsNotificationCredentialStore
from hugin.adapters.notifications import (
    EmailNotificationSender,
    NotificationContent,
    TelegramGatewayNotificationSender,
    WindowsToastSender,
)
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.diagnostics import OperationJournal, error_details
from hugin.domain.communications import NotificationRecord
from hugin.domain.content import NotificationChannel
from hugin.repositories.communications import CommunicationRepository
from hugin.services.notifications import NotificationService


class NotificationWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        poll_seconds: float = 2.0,
        journal: OperationJournal | None = None,
    ) -> None:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")
        if poll_seconds <= 0:
            raise ValueError("Интервал проверки уведомлений должен быть положительным")
        self._settings = settings
        self._account_id = account_id
        self._poll_seconds = poll_seconds
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
            "notifications",
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
            name="hugin-notifications",
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
            "notifications",
            "worker.lifecycle",
            status="completed",
            action="stop",
            account_id=self._account_id,
        )

    def run_once(self, now: datetime | None = None) -> bool:
        selected_at = now or datetime.now(UTC)
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                NotificationService(session).collect(self._account_id, selected_at)
                notification = CommunicationRepository(session).claim_due_notification(selected_at)
        finally:
            database.close()
        if notification is None:
            return False

        run = self._journal.start(
            "notifications",
            "send",
            account_id=self._account_id,
            notification_id=notification.id,
            application_id=notification.application_id,
            incident_id=notification.incident_id,
            event_type=notification.event_type,
            channel=notification.channel.value,
        )
        try:
            self._send(notification)
        except Exception as error:
            self._record_failure(
                notification.id,
                type(error).__name__,
                selected_at + timedelta(minutes=5),
            )
            run.fail(error, retry_in_minutes=5)
        else:
            self._record_success(notification.id, selected_at)
            run.succeed()
        return True

    def _send(self, notification: NotificationRecord) -> None:
        title = notification.payload.get("title")
        body = notification.payload.get("body")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("У уведомления отсутствует заголовок")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("У уведомления отсутствует текст")
        content = NotificationContent(title.strip(), body.strip())
        if notification.channel is NotificationChannel.WINDOWS:
            WindowsToastSender().send(content)
            return
        credentials = WindowsNotificationCredentialStore()
        if notification.channel is NotificationChannel.TELEGRAM:
            telegram = credentials.load_telegram_gateway()
            if telegram is None:
                raise RuntimeError("Telegram не настроен")
            TelegramGatewayNotificationSender(
                self._settings.telegram_gateway_url,
                telegram,
                timeout_seconds=self._settings.telegram_gateway_timeout_seconds,
            ).send(content)
            return
        email = credentials.load_email()
        if email is None:
            raise RuntimeError("Электронная почта не настроена")
        EmailNotificationSender(email).send(content)

    def _record_success(self, notification_id: int, sent_at: datetime) -> None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                CommunicationRepository(session).mark_notification_sent(
                    notification_id,
                    sent_at,
                )
        finally:
            database.close()

    def _record_failure(
        self,
        notification_id: int,
        error_code: str,
        retry_at: datetime,
    ) -> None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                CommunicationRepository(session).mark_notification_failed(
                    notification_id,
                    error_code=error_code,
                    retry_at=retry_at,
                )
        finally:
            database.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception as error:
                self._journal.record(
                    "notifications",
                    "worker.loop",
                    status="failed",
                    level="ERROR",
                    account_id=self._account_id,
                    **error_details(error),
                )
                worked = False
            if not worked:
                self._stop.wait(self._poll_seconds)
