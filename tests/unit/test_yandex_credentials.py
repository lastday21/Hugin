from __future__ import annotations

import json
import sys

import pytest

from hugin.adapters import yandex_credentials as credentials_module
from hugin.adapters.yandex_credentials import (
    WindowsYandexAICredentialStore,
    YandexAICredentials,
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


def test_yandex_credentials_round_trip_without_exposing_key() -> None:
    backend = FakeKeyring()
    store = WindowsYandexAICredentialStore(backend)
    credentials = YandexAICredentials(" secret ", " folder ", " model ")

    store.save(credentials)

    payload = backend.values[("Hugin.yandex_ai", "configuration")]
    assert json.loads(payload) == {
        "api_key": "secret",
        "folder_id": "folder",
        "model": "model",
    }
    assert store.load() == YandexAICredentials("secret", "folder", "model")
    assert "secret" not in repr(credentials)
    assert store.delete()
    assert store.load() is None
    assert not store.delete()


def test_yandex_credentials_reject_corrupted_value() -> None:
    backend = FakeKeyring()
    backend.values[("Hugin.yandex_ai", "configuration")] = "{}"

    with pytest.raises(RuntimeError, match="повреждены"):
        WindowsYandexAICredentialStore(backend).load()


def test_default_yandex_store_is_windows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="только в Windows"):
        WindowsYandexAICredentialStore().load()


def test_default_yandex_store_uses_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeKeyring()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(credentials_module, "import_module", lambda _name: backend)
    store = WindowsYandexAICredentialStore()

    store.save(YandexAICredentials("key", "folder"))

    assert store.load() == YandexAICredentials("key", "folder")
