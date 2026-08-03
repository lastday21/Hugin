from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast


@dataclass(frozen=True, slots=True)
class NotificationGatewayCredentials:
    service_key: str

    def __post_init__(self) -> None:
        selected = self.service_key.strip()
        if len(selected) < 32 or len(selected) > 512:
            raise ValueError("Некорректный ключ службы уведомлений")
        object.__setattr__(self, "service_key", selected)

    def __repr__(self) -> str:
        return "NotificationGatewayCredentials(service_key='***')"


class KeyringBackend(Protocol):
    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class WindowsNotificationCredentialStore:
    _NOTIFICATION_GATEWAY_KEY = "notification.service_key"
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

    def save_notification_gateway(
        self,
        credentials: NotificationGatewayCredentials,
    ) -> None:
        self._get_backend().set_password(
            self._service_name,
            self._NOTIFICATION_GATEWAY_KEY,
            credentials.service_key,
        )
        self._delete_legacy_credentials()

    def load_notification_gateway(self) -> NotificationGatewayCredentials | None:
        service_key = self._load_required_text(
            self._NOTIFICATION_GATEWAY_KEY,
            "Сохранённый ключ службы уведомлений повреждён",
        )
        if service_key is None:
            return None
        try:
            return NotificationGatewayCredentials(service_key)
        except ValueError as error:
            raise RuntimeError("Сохранённый ключ службы уведомлений повреждён") from error

    def delete_notification_gateway(self) -> bool:
        return self._delete(self._NOTIFICATION_GATEWAY_KEY)

    def _delete_legacy_credentials(self) -> None:
        for key in (
            self._TELEGRAM_GATEWAY_KEY,
            self._TELEGRAM_TOKEN_KEY,
            self._TELEGRAM_CHAT_KEY,
            self._TELEGRAM_LEGACY_KEY,
            "email",
        ):
            self._delete(key)

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


def load_notification_gateway_credentials(
    key_file: Path | None,
    store: WindowsNotificationCredentialStore | None = None,
) -> NotificationGatewayCredentials | None:
    if key_file is not None:
        try:
            service_key = key_file.read_text(encoding="utf-8-sig").strip()
        except OSError as error:
            raise RuntimeError("Файл ключа службы уведомлений недоступен") from error
        try:
            return NotificationGatewayCredentials(service_key)
        except ValueError as error:
            raise RuntimeError("Файл ключа службы уведомлений повреждён") from error
    selected_store = store or WindowsNotificationCredentialStore()
    return selected_store.load_notification_gateway()
