from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, cast


@dataclass(frozen=True, slots=True)
class YandexAICredentials:
    api_key: str
    folder_id: str
    model: str = "aliceai-llm/latest"

    def __repr__(self) -> str:
        return (
            "YandexAICredentials(api_key='***', "
            f"folder_id={self.folder_id!r}, model={self.model!r})"
        )


class KeyringBackend(Protocol):
    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class WindowsYandexAICredentialStore:
    def __init__(
        self,
        backend: KeyringBackend | None = None,
        service_name: str = "Hugin.yandex_ai",
    ) -> None:
        self._backend = backend
        self._service_name = service_name

    def save(self, credentials: YandexAICredentials) -> None:
        api_key = credentials.api_key.strip()
        folder_id = credentials.folder_id.strip()
        model = credentials.model.strip()
        if not api_key or not folder_id or not model:
            raise ValueError("Настройки YandexGPT заполнены не полностью")
        payload = json.dumps(
            {"api_key": api_key, "folder_id": folder_id, "model": model},
            ensure_ascii=False,
        )
        self._get_backend().set_password(self._service_name, "configuration", payload)

    def load(self) -> YandexAICredentials | None:
        payload = self._get_backend().get_password(self._service_name, "configuration")
        if payload is None:
            return None
        try:
            value = json.loads(payload)
            api_key = value["api_key"]
            folder_id = value["folder_id"]
            model = value["model"]
            if not all(
                isinstance(item, str) and item.strip() for item in (api_key, folder_id, model)
            ):
                raise TypeError
            return YandexAICredentials(api_key.strip(), folder_id.strip(), model.strip())
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("Сохраненные настройки YandexGPT повреждены") from error

    def delete(self) -> bool:
        backend = self._get_backend()
        if backend.get_password(self._service_name, "configuration") is None:
            return False
        backend.delete_password(self._service_name, "configuration")
        return True

    def _get_backend(self) -> KeyringBackend:
        if self._backend is not None:
            return self._backend
        if sys.platform != "win32":
            raise RuntimeError("Защищенное хранилище YandexGPT поддерживается только в Windows")
        return cast(KeyringBackend, import_module("keyring"))
