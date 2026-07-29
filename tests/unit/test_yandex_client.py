from __future__ import annotations

from typing import cast

import pytest
from pydantic import SecretStr

from hugin.adapters.yandex_credentials import (
    WindowsYandexAICredentialStore,
    YandexAICredentials,
)
from hugin.core.settings import Settings
from hugin.services.yandex_client import configured_yandex_ai_client


class FakeStore:
    def __init__(self, credentials: YandexAICredentials | None) -> None:
        self._credentials = credentials

    def load(self) -> YandexAICredentials | None:
        return self._credentials


def test_yandex_client_uses_environment_configuration() -> None:
    settings = Settings(
        environment="test",
        yandex_ai_api_key=SecretStr("secret"),
        yandex_ai_folder_id="folder",
        yandex_ai_model="model/latest",
    )

    client = configured_yandex_ai_client(settings)

    assert client.model_name == "model/latest"


def test_yandex_client_uses_protected_store() -> None:
    settings = Settings(environment="test")
    store = FakeStore(YandexAICredentials("secret", "folder", "stored/latest"))

    client = configured_yandex_ai_client(
        settings,
        cast(WindowsYandexAICredentialStore, store),
    )

    assert client.model_name == "stored/latest"


def test_yandex_client_uses_selected_model() -> None:
    settings = Settings(environment="test")
    store = FakeStore(YandexAICredentials("secret", "folder", "stored/latest"))

    client = configured_yandex_ai_client(
        settings,
        cast(WindowsYandexAICredentialStore, store),
        model="qwen3-235b-a22b-fp8/latest",
        reasoning_effort="high",
    )

    assert client.model_name == "qwen3-235b-a22b-fp8/latest"


def test_yandex_client_requires_complete_configuration() -> None:
    with pytest.raises(ValueError, match="FOLDER_ID"):
        configured_yandex_ai_client(
            Settings(environment="test", yandex_ai_api_key=SecretStr("secret")),
        )
    with pytest.raises(LookupError, match="не настроен"):
        configured_yandex_ai_client(
            Settings(environment="test"),
            cast(WindowsYandexAICredentialStore, FakeStore(None)),
        )
