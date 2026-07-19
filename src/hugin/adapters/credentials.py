from __future__ import annotations

import json
import sys
from importlib import import_module
from typing import Protocol, cast

from hugin.services.hh_login import HhCredentials


class KeyringBackend(Protocol):
    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class WindowsCredentialStore:
    def __init__(
        self,
        backend: KeyringBackend | None = None,
        service_name: str = "Hugin.hh.ru",
    ) -> None:
        self._backend = backend
        self._service_name = service_name

    def save(self, account_id: int, credentials: HhCredentials) -> None:
        payload = json.dumps(
            {"login": credentials.login.strip(), "password": credentials.password},
            ensure_ascii=False,
        )
        self._get_backend().set_password(self._service_name, self._key(account_id), payload)

    def load(self, account_id: int) -> HhCredentials | None:
        payload = self._get_backend().get_password(self._service_name, self._key(account_id))
        if payload is None:
            return None

        try:
            value = json.loads(payload)
            login = value["login"]
            password = value["password"]
            if not isinstance(login, str) or not isinstance(password, str):
                raise TypeError
            return HhCredentials(login=login, password=password)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("Сохранённые данные hh.ru повреждены") from error

    def delete(self, account_id: int) -> bool:
        backend = self._get_backend()
        key = self._key(account_id)
        if backend.get_password(self._service_name, key) is None:
            return False
        backend.delete_password(self._service_name, key)
        return True

    def _get_backend(self) -> KeyringBackend:
        if self._backend is not None:
            return self._backend
        if sys.platform != "win32":
            raise RuntimeError("Защищённое хранилище данных hh.ru поддерживается только в Windows")
        return cast(KeyringBackend, import_module("keyring"))

    @staticmethod
    def _key(account_id: int) -> str:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")
        return f"account:{account_id}"
