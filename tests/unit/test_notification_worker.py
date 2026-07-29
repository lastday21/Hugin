from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hugin.adapters.notifications import NotificationContent
from hugin.core.settings import Settings
from hugin.domain.communications import NotificationRecord
from hugin.domain.content import DeliveryState, NotificationChannel
from hugin.workers import notifications as worker_module


def notification(channel: NotificationChannel) -> NotificationRecord:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    return NotificationRecord(
        id=1,
        application_id=None,
        incident_id=None,
        deduplication_key=f"test:{channel.value}",
        event_type="NEW_MESSAGE",
        channel=channel,
        state=DeliveryState.PENDING,
        payload={"title": "Hugin", "body": "Новое сообщение"},
        scheduled_at=now,
        sent_at=None,
        error_code=None,
        created_at=now,
    )


def test_worker_routes_each_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str]] = []

    class Sender:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def send(self, content: NotificationContent) -> None:
            sent.append((type(self).__name__, content.body))

    class Credentials:
        def load_telegram_gateway(self) -> object:
            return object()

        def load_email(self) -> object:
            return object()

    monkeypatch.setattr(worker_module, "WindowsToastSender", Sender)
    monkeypatch.setattr(worker_module, "TelegramGatewayNotificationSender", Sender)
    monkeypatch.setattr(worker_module, "EmailNotificationSender", Sender)
    monkeypatch.setattr(worker_module, "WindowsNotificationCredentialStore", Credentials)
    worker = worker_module.NotificationWorker(Settings(environment="test"))

    worker._send(notification(NotificationChannel.WINDOWS))
    worker._send(notification(NotificationChannel.TELEGRAM))
    worker._send(notification(NotificationChannel.EMAIL))

    assert len(sent) == 3
    assert all(body == "Новое сообщение" for _sender, body in sent)


def test_worker_requires_connected_telegram_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Credentials:
        def load_telegram_gateway(self) -> None:
            return None

    monkeypatch.setattr(worker_module, "WindowsNotificationCredentialStore", Credentials)
    worker = worker_module.NotificationWorker(Settings(environment="test"))

    with pytest.raises(RuntimeError, match="Telegram не настроен"):
        worker._send(notification(NotificationChannel.TELEGRAM))


def test_worker_retries_failed_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selected = notification(NotificationChannel.WINDOWS)
    recorded: list[tuple[str, object]] = []
    worker = worker_module.NotificationWorker(Settings(environment="test", data_dir=tmp_path))
    monkeypatch.setattr(
        worker,
        "_send",
        lambda _notification: (_ for _ in ()).throw(RuntimeError("temporary")),
    )
    monkeypatch.setattr(
        worker,
        "_record_failure",
        lambda notification_id, error_code, retry_at: recorded.append(
            ("failed", (notification_id, error_code, retry_at))
        ),
    )
    monkeypatch.setattr(
        worker,
        "_record_success",
        lambda notification_id, sent_at: recorded.append(("sent", (notification_id, sent_at))),
    )

    class Sessions:
        def begin(self) -> object:
            class Context:
                def __enter__(self) -> object:
                    return object()

                def __exit__(self, *_args: object) -> None:
                    pass

            return Context()

    class Database:
        sessions = Sessions()

        def close(self) -> None:
            pass

    class Service:
        def __init__(self, _session: object) -> None:
            pass

        def collect(self, account_id: int, now: datetime) -> int:
            assert account_id == 1
            return 0

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def claim_due_notification(self, _now: datetime) -> NotificationRecord:
            return selected

    monkeypatch.setattr(worker_module, "create_database", lambda _settings: Database())
    monkeypatch.setattr(worker_module, "NotificationService", Service)
    monkeypatch.setattr(worker_module, "CommunicationRepository", Repository)
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)

    assert worker.run_once(now)
    assert recorded[0][0] == "failed"


def test_worker_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        worker_module.NotificationWorker(Settings(environment="test"), account_id=0)
    with pytest.raises(ValueError):
        worker_module.NotificationWorker(Settings(environment="test"), poll_seconds=0)


def test_worker_start_stop_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upgraded: list[Settings] = []

    class Thread:
        def __init__(self, **values: object) -> None:
            assert values["name"] == "hugin-notifications"
            assert values["daemon"] is True
            self.alive = False
            self.joined: list[float] = []

        def is_alive(self) -> bool:
            return self.alive

        def start(self) -> None:
            self.alive = True

        def join(self, timeout: float) -> None:
            self.joined.append(timeout)
            self.alive = False

    threads: list[Thread] = []

    def create_thread(**values: object) -> Thread:
        thread = Thread(**values)
        threads.append(thread)
        return thread

    monkeypatch.setattr(worker_module, "upgrade_database", upgraded.append)
    monkeypatch.setattr(threading, "Thread", create_thread)
    settings = Settings(environment="test", data_dir=tmp_path)
    worker = worker_module.NotificationWorker(settings)

    worker.start()
    worker.start()
    assert worker.running
    assert upgraded == [settings]
    assert len(threads) == 1
    worker.stop(0.5)
    worker.stop()
    assert not worker.running
    assert threads[0].joined == [0.5]
