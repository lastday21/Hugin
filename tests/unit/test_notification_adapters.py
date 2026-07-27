# ruff: noqa: RUF001

from __future__ import annotations

import json
from email.message import EmailMessage
from types import SimpleNamespace
from typing import ClassVar, cast
from urllib.request import Request

import pytest

from hugin.adapters import notifications as notification_module
from hugin.adapters.notification_credentials import (
    EmailCredentials,
    TelegramCredentials,
    WindowsNotificationCredentialStore,
)
from hugin.adapters.notifications import (
    EmailNotificationSender,
    NotificationContent,
    TelegramNotificationSender,
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
    telegram = TelegramCredentials("token:secret", "-100123")
    email = EmailCredentials(
        "smtp.example.com",
        587,
        "user@example.com",
        "secret",
        "sender@example.com",
        "recipient@example.com",
    )

    store.save_telegram(telegram)
    store.save_email(email)

    assert store.load_telegram() == telegram
    assert store.load_email() == email
    assert "secret" not in repr(telegram)
    assert "secret" not in repr(email)
    assert store.delete_telegram()
    assert not store.delete_telegram()
    assert store.delete_email()


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


def test_telegram_sender_requires_positive_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"ok":true,"result":{"message_id":7}}'

    def urlopen(request: object, *, timeout: int) -> Response:
        assert timeout == 15
        payload = cast(Request, request).data
        assert payload is not None
        captured.append(json.loads(cast(bytes, payload)))
        return Response()

    monkeypatch.setattr(notification_module, "urlopen", urlopen)
    TelegramNotificationSender(TelegramCredentials("token", "chat")).send(
        NotificationContent("Приглашение", "Отклик: Python")
    )

    assert captured == [
        {
            "chat_id": "chat",
            "text": "Приглашение\n\nОтклик: Python",
            "disable_web_page_preview": True,
        }
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
