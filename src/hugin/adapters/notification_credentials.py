from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, cast


@dataclass(frozen=True, slots=True)
class TelegramGatewayCredentials:
    access_token: str

    def __repr__(self) -> str:
        return "TelegramGatewayCredentials(access_token='***')"


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
    _TELEGRAM_LEGACY_KEY = "telegram"
    _TELEGRAM_TOKEN_KEY = "telegram.bot_token"
    _TELEGRAM_CHAT_KEY = "telegram.chat_id"
    _TELEGRAM_GATEWAY_KEY = "telegram.gateway_access_token"

    def __init__(
        self,
        backend: KeyringBackend | None = None,
        service_name: str = "Hugin.notifications",
    ) -> None:
        self._backend = backend
        self._service_name = service_name

    def save_telegram_gateway(self, credentials: TelegramGatewayCredentials) -> None:
        access_token = credentials.access_token.strip()
        if not access_token:
            raise ValueError("Служба Telegram не выдала ключ подключения")
        self._get_backend().set_password(
            self._service_name,
            self._TELEGRAM_GATEWAY_KEY,
            access_token,
        )
        self._delete_direct_telegram_credentials()

    def load_telegram_gateway(self) -> TelegramGatewayCredentials | None:
        access_token = self._load_required_text(
            self._TELEGRAM_GATEWAY_KEY,
            "Сохранённое подключение Telegram повреждено",
        )
        if access_token is None:
            return None
        return TelegramGatewayCredentials(access_token)

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
        deleted = [
            self._delete(self._TELEGRAM_GATEWAY_KEY),
            self._delete(self._TELEGRAM_TOKEN_KEY),
            self._delete(self._TELEGRAM_CHAT_KEY),
            self._delete(self._TELEGRAM_LEGACY_KEY),
        ]
        return any(deleted)

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

    def _delete_direct_telegram_credentials(self) -> bool:
        deleted = [
            self._delete(self._TELEGRAM_TOKEN_KEY),
            self._delete(self._TELEGRAM_CHAT_KEY),
            self._delete(self._TELEGRAM_LEGACY_KEY),
        ]
        return any(deleted)

    def _load_required_text(self, key: str, error_message: str) -> str | None:
        value = self._get_backend().get_password(self._service_name, key)
        if value is None:
            return None
        selected = value.strip()
        if not selected:
            raise RuntimeError(error_message)
        return selected

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
