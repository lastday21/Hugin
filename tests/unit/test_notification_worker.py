from __future__ import annotations

import threading
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

import hugin.workers.notifications as worker_module
from hugin.adapters.notifications import NotificationContent
from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal
from hugin.domain.communications import NotificationRecord
from hugin.domain.content import DeliveryState, NotificationChannel


def notification(
    channel: NotificationChannel,
    *,
    action_url: str | None = None,
) -> NotificationRecord:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    payload: dict[str, object] = {"title": "Hugin", "body": "Новое сообщение"}
    if action_url is not None:
        payload["action_url"] = action_url
    return NotificationRecord(
        id=1,
        application_id=None,
        incident_id=None,
        deduplication_key=f"test:{channel.value}",
        event_type="NEW_MESSAGE",
        channel=channel,
        state=DeliveryState.PENDING,
        payload=payload,
        scheduled_at=now,
        sent_at=None,
        error_code=None,
        created_at=now,
    )


def test_worker_routes_each_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows: list[NotificationContent] = []
    gateway_calls: list[dict[str, object]] = []
    gateway_initialization: list[tuple[str, object, int]] = []
    credentials = object()

    class WindowsSender:
        def send(self, content: NotificationContent) -> None:
            windows.append(content)

    class GatewaySender:
        def __init__(
            self,
            base_url: str,
            selected_credentials: object,
            *,
            timeout_seconds: int,
        ) -> None:
            gateway_initialization.append((base_url, selected_credentials, timeout_seconds))

        def send(self, **values: object) -> None:
            gateway_calls.append(values)

    monkeypatch.setattr(worker_module, "WindowsToastSender", WindowsSender)
    monkeypatch.setattr(worker_module, "NotificationGatewaySender", GatewaySender)
    monkeypatch.setattr(
        worker_module,
        "load_notification_gateway_credentials",
        lambda _path: credentials,
    )
    worker = worker_module.NotificationWorker(Settings(environment="test"))

    worker._send(notification(NotificationChannel.WINDOWS))
    telegram = notification(
        NotificationChannel.TELEGRAM,
        action_url="https://hh.ru/vacancy/101",
    )
    worker._send(telegram)
    worker._send(notification(NotificationChannel.EMAIL))

    assert windows == [NotificationContent("Hugin", "Новое сообщение")]
    assert gateway_initialization == [
        (
            worker._settings.notification_gateway_url,
            credentials,
            worker._settings.notification_gateway_timeout_seconds,
        ),
        (
            worker._settings.notification_gateway_url,
            credentials,
            worker._settings.notification_gateway_timeout_seconds,
        ),
    ]
    assert [call["channel"] for call in gateway_calls] == ["TELEGRAM", "EMAIL"]
    assert gateway_calls[0]["action_url"] == "https://hh.ru/vacancy/101"
    assert gateway_calls[1]["action_url"] is None
    assert gateway_calls[0]["event_id"] == (
        "hugin:" + sha256(telegram.deduplication_key.encode("utf-8")).hexdigest()
    )
    assert telegram.deduplication_key not in str(gateway_calls[0]["event_id"])


@pytest.mark.parametrize(
    "channel",
    [NotificationChannel.TELEGRAM, NotificationChannel.EMAIL],
)
def test_worker_requires_notification_service_credentials(
    monkeypatch: pytest.MonkeyPatch,
    channel: NotificationChannel,
) -> None:
    monkeypatch.setattr(
        worker_module,
        "load_notification_gateway_credentials",
        lambda _path: None,
    )
    worker = worker_module.NotificationWorker(Settings(environment="test"))

    with pytest.raises(
        worker_module.NotificationChannelNotConfigured,
        match="Служба внешних уведомлений не настроена",
    ) as error:
        worker._send(notification(channel))
    assert error.value.code == "NOTIFICATION_SERVICE_NOT_CONFIGURED"


def test_worker_treats_unreadable_service_key_as_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load(_path: Path | None) -> None:
        raise RuntimeError("Файл служебного ключа недоступен")

    monkeypatch.setattr(worker_module, "load_notification_gateway_credentials", load)
    worker = worker_module.NotificationWorker(Settings(environment="test"))

    with pytest.raises(
        worker_module.NotificationChannelNotConfigured,
        match="Файл служебного ключа недоступен",
    ) as error:
        worker._send(notification(NotificationChannel.EMAIL))
    assert error.value.code == "NOTIFICATION_SERVICE_NOT_CONFIGURED"


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


def test_worker_blocks_unconfigured_channel_and_records_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selected = notification(NotificationChannel.EMAIL)
    recorded: list[tuple[int, str, datetime]] = []
    journal = OperationJournal(tmp_path)
    worker = worker_module.NotificationWorker(
        Settings(environment="test", data_dir=tmp_path),
        journal=journal,
    )
    monkeypatch.setattr(
        worker,
        "_send",
        lambda _notification: (_ for _ in ()).throw(
            worker_module.NotificationChannelNotConfigured(
                "NOTIFICATION_SERVICE_NOT_CONFIGURED",
                "Служба внешних уведомлений не настроена",
            )
        ),
    )
    monkeypatch.setattr(
        worker,
        "_record_failure",
        lambda notification_id, error_code, retry_at: recorded.append(
            (notification_id, error_code, retry_at)
        ),
    )
    monkeypatch.setattr(
        worker,
        "_record_success",
        lambda *_args: pytest.fail("Успешная доставка не должна записываться"),
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
    assert recorded == [(selected.id, "NOTIFICATION_SERVICE_NOT_CONFIGURED", now)]
    entries = list(journal.entries(component="notifications"))
    assert [entry["status"] for entry in entries] == ["started", "blocked"]
    assert entries[-1]["details"]["error_code"] == "NOTIFICATION_SERVICE_NOT_CONFIGURED"
    assert entries[-1]["details"]["reason"] == "Служба внешних уведомлений не настроена"


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
