from __future__ import annotations

import json
import sys

import pytest

from hugin.adapters import credentials as credentials_module
from hugin.adapters.credentials import WindowsCredentialStore
from hugin.services.hh_login import HhCredentials


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        del self.values[(service_name, username)]


def test_credentials_round_trip_through_keyring() -> None:
    backend = FakeKeyring()
    store = WindowsCredentialStore(backend)

    store.save(7, HhCredentials("  person@example.com  ", "secret"))

    saved = backend.values[("Hugin.hh.ru", "account:7")]
    assert json.loads(saved) == {"login": "person@example.com", "password": "secret"}
    assert store.load(7) == HhCredentials("person@example.com", "secret")
    assert store.delete(7)
    assert store.load(7) is None
    assert not store.delete(7)


@pytest.mark.parametrize("payload", ["not-json", "{}", '{"login": null, "password": "x"}'])
def test_corrupted_credentials_are_rejected(payload: str) -> None:
    backend = FakeKeyring()
    backend.values[("Hugin.hh.ru", "account:1")] = payload

    with pytest.raises(RuntimeError, match="повреждены"):
        WindowsCredentialStore(backend).load(1)


def test_account_id_must_be_positive() -> None:
    with pytest.raises(ValueError):
        WindowsCredentialStore(FakeKeyring()).load(0)


def test_default_store_rejects_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="только в Windows"):
        WindowsCredentialStore().load(1)


def test_default_store_loads_keyring_module(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeKeyring()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(credentials_module, "import_module", lambda name: backend)

    store = WindowsCredentialStore()
    store.save(1, HhCredentials("person@example.com", "secret"))

    assert store.load(1) == HhCredentials("person@example.com", "secret")
