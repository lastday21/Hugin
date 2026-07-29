from __future__ import annotations

import json
from email.message import EmailMessage
from types import SimpleNamespace
from typing import ClassVar

import pytest

from hugin.adapters.notification_credentials import (
    EmailCredentials,
    TelegramGatewayCredentials,
    WindowsNotificationCredentialStore,
)
from hugin.adapters.notifications import (
    EmailNotificationSender,
    NotificationContent,
    TelegramGatewayNotificationSender,
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
    telegram = TelegramGatewayCredentials("hgt_connection.secret")
    email = EmailCredentials(
        "smtp.example.com",
        587,
        "user@example.com",
        "secret",
        "sender@example.com",
        "recipient@example.com",
    )

    store.save_telegram_gateway(telegram)
    store.save_email(email)

    assert (
        backend.values[("Hugin.notifications", "telegram.gateway_access_token")]
        == "hgt_connection.secret"
    )
    assert ("Hugin.notifications", "telegram") not in backend.values
    assert store.load_telegram_gateway() == telegram
    assert store.load_email() == email
    assert "secret" not in repr(telegram)
    assert "secret" not in repr(email)
    assert store.delete_telegram()
    assert not store.delete_telegram()
    assert store.delete_email()


def test_gateway_connection_removes_old_direct_telegram_secrets() -> None:
    backend = FakeKeyring()
    store = WindowsNotificationCredentialStore(backend)
    backend.values[("Hugin.notifications", "telegram.bot_token")] = "old-token"
    backend.values[("Hugin.notifications", "telegram.chat_id")] = "123"
    backend.values[("Hugin.notifications", "telegram")] = "old-payload"

    credentials = TelegramGatewayCredentials("hgt_connection.secret")
    store.save_telegram_gateway(credentials)

    assert store.load_telegram_gateway() == credentials
    assert ("Hugin.notifications", "telegram.bot_token") not in backend.values
    assert ("Hugin.notifications", "telegram.chat_id") not in backend.values
    assert ("Hugin.notifications", "telegram") not in backend.values


def test_gateway_connection_rejects_empty_and_corrupted_values() -> None:
    backend = FakeKeyring()
    store = WindowsNotificationCredentialStore(backend)

    with pytest.raises(ValueError, match="ключ подключения"):
        store.save_telegram_gateway(TelegramGatewayCredentials(" "))

    backend.values[("Hugin.notifications", "telegram.gateway_access_token")] = " "
    with pytest.raises(RuntimeError, match="повреждено"):
        store.load_telegram_gateway()


def test_notification_store_rejects_invalid_email_payloads() -> None:
    backend = FakeKeyring()
    store = WindowsNotificationCredentialStore(backend)

    with pytest.raises(ValueError, match="не полностью"):
        store.save_email(EmailCredentials("", 587, "", "", "", "to@example.com"))
    with pytest.raises(ValueError, match="порт"):
        store.save_email(
            EmailCredentials(
                "smtp.example.com",
                0,
                "user",
                "password",
                "from@example.com",
                "to@example.com",
            )
        )
    assert store.load_email() is None

    backend.values[("Hugin.notifications", "email")] = "{"
    with pytest.raises(RuntimeError, match="повреждены"):
        store.load_email()
    backend.values[("Hugin.notifications", "email")] = "[]"
    with pytest.raises(RuntimeError, match="повреждены"):
        store.load_email()

    malformed = {
        "smtp_host": "",
        "smtp_port": 587,
        "username": "user",
        "password": "password",
        "sender": "from@example.com",
        "recipient": "to@example.com",
        "starttls": True,
    }
    for key, value in (
        ("smtp_host", ""),
        ("smtp_port", 0),
        ("username", 1),
        ("sender", 1),
        ("recipient", ""),
        ("starttls", "yes"),
    ):
        payload = {**malformed, "smtp_host": "smtp.example.com", key: value}
        backend.values[("Hugin.notifications", "email")] = json.dumps(payload)
        with pytest.raises(RuntimeError, match="повреждены"):
            store.load_email()


def test_notification_store_requires_windows_without_injected_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hugin.adapters.notification_credentials.sys.platform", "linux")

    with pytest.raises(RuntimeError, match="Windows"):
        WindowsNotificationCredentialStore().load_telegram_gateway()


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


def test_telegram_sender_uses_gateway_without_bot_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, int, str, str, str]] = []

    class Client:
        def __init__(self, gateway_url: str, *, timeout_seconds: int) -> None:
            self.gateway_url = gateway_url
            self.timeout_seconds = timeout_seconds

        def send(self, access_token: str, title: str, body: str) -> None:
            captured.append((self.gateway_url, self.timeout_seconds, access_token, title, body))

    monkeypatch.setattr("hugin.adapters.notifications.TelegramGatewayClient", Client)
    TelegramGatewayNotificationSender(
        "https://telegram.example",
        TelegramGatewayCredentials("hgt_connection.secret"),
    ).send(NotificationContent("Приглашение", "Отклик: Python"))

    assert captured == [
        (
            "https://telegram.example",
            15,
            "hgt_connection.secret",
            "Приглашение",
            "Отклик: Python",
        )
    ]


def test_email_sender_checks_rejected_recipients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[EmailMessage] = []

    class FakeSmtp:
        refused: ClassVar[dict[str, object]] = {}

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeSmtp:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def starttls(self, **_kwargs: object) -> None:
            pass

        def login(self, username: str, password: str) -> None:
            assert (username, password) == ("user", "password")

        def send_message(self, message: EmailMessage) -> dict[str, object]:
            sent.append(message)
            return self.refused

    monkeypatch.setattr("hugin.adapters.notifications.smtplib.SMTP", FakeSmtp)
    credentials = EmailCredentials(
        "smtp.example.com",
        587,
        "user",
        "password",
        "from@example.com",
        "to@example.com",
    )
    EmailNotificationSender(credentials).send(NotificationContent("Hugin", "Итоги"))
    assert sent[0]["To"] == "to@example.com"

    FakeSmtp.refused = {"to@example.com": object()}
    with pytest.raises(RuntimeError, match="отклонил"):
        EmailNotificationSender(credentials).send(NotificationContent("Hugin", "Итоги"))
