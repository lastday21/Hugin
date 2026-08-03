from __future__ import annotations

from types import SimpleNamespace

import pytest

from hugin.adapters.notification_credentials import (
    NotificationGatewayCredentials,
    WindowsNotificationCredentialStore,
)
from hugin.adapters.notifications import (
    NotificationContent,
    NotificationGatewaySender,
    WindowsToastSender,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        del self.values[(service_name, username)]


def test_notification_credentials_round_trip_without_exposing_secrets() -> None:
    backend = FakeKeyring()
    store = WindowsNotificationCredentialStore(backend)
    credentials = NotificationGatewayCredentials("s" * 32)
    backend.values[("Hugin.notifications", "telegram.bot_token")] = "old-token"
    backend.values[("Hugin.notifications", "telegram.chat_id")] = "123"
    backend.values[("Hugin.notifications", "telegram")] = "old-payload"
    backend.values[("Hugin.notifications", "telegram.gateway_access_token")] = "old-key"
    backend.values[("Hugin.notifications", "email")] = "old-email"

    store.save_notification_gateway(credentials)

    assert store.load_notification_gateway() == credentials
    assert "s" * 32 not in repr(credentials)
    assert ("Hugin.notifications", "telegram.bot_token") not in backend.values
    assert ("Hugin.notifications", "telegram.chat_id") not in backend.values
    assert ("Hugin.notifications", "telegram") not in backend.values
    assert ("Hugin.notifications", "telegram.gateway_access_token") not in backend.values
    assert ("Hugin.notifications", "email") not in backend.values
    assert store.delete_notification_gateway()
    assert not store.delete_notification_gateway()


def test_gateway_connection_rejects_empty_and_corrupted_values() -> None:
    backend = FakeKeyring()
    store = WindowsNotificationCredentialStore(backend)

    with pytest.raises(ValueError, match="ключ службы"):
        NotificationGatewayCredentials("short")

    backend.values[("Hugin.notifications", "notification.service_key")] = " "
    with pytest.raises(RuntimeError, match="повреждён"):
        store.load_notification_gateway()


def test_notification_store_requires_windows_without_injected_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hugin.adapters.notification_credentials.sys.platform", "linux")

    with pytest.raises(RuntimeError, match="Windows"):
        WindowsNotificationCredentialStore().load_notification_gateway()


def test_windows_sender_accepts_only_successful_system_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("hugin.adapters.notifications.subprocess.run", run)
    WindowsToastSender().send(NotificationContent("Hugin", "Новое сообщение"))

    assert calls[0][0] == "powershell.exe"
    assert "-EncodedCommand" in calls[0]

    monkeypatch.setattr(
        "hugin.adapters.notifications.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    with pytest.raises(RuntimeError, match="Windows"):
        WindowsToastSender().send(NotificationContent("Hugin", "Ошибка"))


def test_notification_sender_uses_shared_gateway_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[object, ...]] = []
    credentials = NotificationGatewayCredentials("s" * 32)

    class Client:
        def __init__(
            self,
            gateway_url: str,
            selected_credentials: NotificationGatewayCredentials,
            timeout_seconds: int,
        ) -> None:
            self.gateway_url = gateway_url
            self.credentials = selected_credentials
            self.timeout_seconds = timeout_seconds

        def send(
            self,
            event_id: str,
            channel: str,
            event_type: str,
            title: str,
            body: str,
            action_url: str | None,
        ) -> None:
            captured.append(
                (
                    self.gateway_url,
                    self.credentials,
                    self.timeout_seconds,
                    event_id,
                    channel,
                    event_type,
                    title,
                    body,
                    action_url,
                )
            )

    monkeypatch.setattr("hugin.adapters.notifications.NotificationGatewayClient", Client)
    NotificationGatewaySender(
        "http://127.0.0.1:8088",
        credentials,
    ).send(
        event_id="hugin:event",
        channel="TELEGRAM",
        event_type="INVITATION",
        content=NotificationContent("Приглашение", "Отклик: Python"),
        action_url="https://hh.ru/vacancy/101",
    )

    assert captured == [
        (
            "http://127.0.0.1:8088",
            credentials,
            15,
            "hugin:event",
            "TELEGRAM",
            "INVITATION",
            "Приглашение",
            "Отклик: Python",
            "https://hh.ru/vacancy/101",
        )
    ]
