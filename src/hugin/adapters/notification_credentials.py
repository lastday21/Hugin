from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, cast


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    bot_token: str
    chat_id: str

    def __repr__(self) -> str:
        return "TelegramCredentials(bot_token='***', chat_id='***')"


@dataclass(frozen=True, slots=True)
class EmailCredentials:
    smtp_host: str
    smtp_port: int
    username: str
    password: str
    sender: str
    recipient: str
    starttls: bool = True

    def __repr__(self) -> str:
        return (
            "EmailCredentials(smtp_host="
            f"{self.smtp_host!r}, smtp_port={self.smtp_port}, username='***', "
            "password='***', sender='***', recipient='***', "
            f"starttls={self.starttls})"
        )


class KeyringBackend(Protocol):
    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class WindowsNotificationCredentialStore:
    def __init__(
        self,
        backend: KeyringBackend | None = None,
        service_name: str = "Hugin.notifications",
    ) -> None:
        self._backend = backend
        self._service_name = service_name

    def save_telegram(self, credentials: TelegramCredentials) -> None:
        token = credentials.bot_token.strip()
        chat_id = credentials.chat_id.strip()
        if not token or not chat_id:
            raise ValueError("Укажите токен бота и номер чата Telegram")
        self._save("telegram", {"bot_token": token, "chat_id": chat_id})

    def load_telegram(self) -> TelegramCredentials | None:
        value = self._load("telegram")
        if value is None:
            return None
        try:
            token = value["bot_token"]
            chat_id = value["chat_id"]
            if not isinstance(token, str) or not token.strip():
                raise TypeError
            if not isinstance(chat_id, str) or not chat_id.strip():
                raise TypeError
            return TelegramCredentials(token.strip(), chat_id.strip())
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Настройки Telegram повреждены") from error

    def save_email(self, credentials: EmailCredentials) -> None:
        host = credentials.smtp_host.strip()
        username = credentials.username.strip()
        sender = credentials.sender.strip()
        recipient = credentials.recipient.strip()
        if not host or not sender or not recipient:
            raise ValueError("Настройки электронной почты заполнены не полностью")
        if credentials.smtp_port < 1 or credentials.smtp_port > 65_535:
            raise ValueError("Некорректный порт почтового сервера")
        self._save(
            "email",
            {
                "smtp_host": host,
                "smtp_port": credentials.smtp_port,
                "username": username,
                "password": credentials.password,
                "sender": sender,
                "recipient": recipient,
                "starttls": credentials.starttls,
            },
        )

    def load_email(self) -> EmailCredentials | None:
        value = self._load("email")
        if value is None:
            return None
        try:
            host = value["smtp_host"]
            port = value["smtp_port"]
            username = value["username"]
            password = value["password"]
            sender = value["sender"]
            recipient = value["recipient"]
            starttls = value["starttls"]
            if not isinstance(host, str) or not host.strip():
                raise TypeError
            if not isinstance(port, int) or not 1 <= port <= 65_535:
                raise TypeError
            if not isinstance(username, str) or not isinstance(password, str):
                raise TypeError
            if not isinstance(sender, str) or not isinstance(recipient, str):
                raise TypeError
            if not sender.strip() or not recipient.strip() or not isinstance(starttls, bool):
                raise TypeError
            return EmailCredentials(
                host.strip(),
                port,
                username.strip(),
                password,
                sender.strip(),
                recipient.strip(),
                starttls,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Настройки электронной почты повреждены") from error

    def delete_telegram(self) -> bool:
        return self._delete("telegram")

    def delete_email(self) -> bool:
        return self._delete("email")

    def _save(self, key: str, value: dict[str, object]) -> None:
        self._get_backend().set_password(
            self._service_name,
            key,
            json.dumps(value, ensure_ascii=False),
        )

    def _load(self, key: str) -> dict[str, object] | None:
        payload = self._get_backend().get_password(self._service_name, key)
        if payload is None:
            return None
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            raise RuntimeError("Сохранённые настройки уведомлений повреждены") from error
        if not isinstance(value, dict):
            raise RuntimeError("Сохранённые настройки уведомлений повреждены")
        return cast(dict[str, object], value)

    def _delete(self, key: str) -> bool:
        backend = self._get_backend()
        if backend.get_password(self._service_name, key) is None:
            return False
        backend.delete_password(self._service_name, key)
        return True

    def _get_backend(self) -> KeyringBackend:
        if self._backend is not None:
            return self._backend
        if sys.platform != "win32":
            raise RuntimeError("Защищённое хранилище уведомлений доступно только в Windows")
        return cast(KeyringBackend, import_module("keyring"))
