from __future__ import annotations

from hugin.adapters.yandex_ai import YandexAIClient
from hugin.adapters.yandex_credentials import (
    WindowsYandexAICredentialStore,
    YandexAICredentials,
)
from hugin.core.settings import Settings


def configured_yandex_ai_client(
    settings: Settings,
    store: WindowsYandexAICredentialStore | None = None,
) -> YandexAIClient:
    environment_key = settings.yandex_ai_api_key.get_secret_value().strip()
    if environment_key:
        if not settings.yandex_ai_folder_id.strip():
            raise ValueError("Для ключа из окружения укажите HUGIN_YANDEX_AI_FOLDER_ID")
        credentials = YandexAICredentials(
            environment_key,
            settings.yandex_ai_folder_id,
            settings.yandex_ai_model,
        )
    else:
        stored_credentials = (store or WindowsYandexAICredentialStore()).load()
        if stored_credentials is None:
            raise LookupError("YandexGPT не настроен; выполните hugin-letters configure")
        credentials = stored_credentials
    return YandexAIClient(
        credentials.api_key,
        credentials.folder_id,
        credentials.model,
        settings.yandex_ai_base_url,
        settings.yandex_ai_timeout_seconds,
    )
