from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    if local_app_data := os.getenv("LOCALAPPDATA"):
        return Path(local_app_data) / "Hugin"
    return Path.home() / ".local" / "share" / "hugin"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="HUGIN_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Hugin"
    environment: Literal["development", "test", "production"] = "development"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    data_dir: Path = Field(default_factory=default_data_dir)
    database_host: str = "127.0.0.1"
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = "hugin"
    database_user: str = "hugin"
    database_password: SecretStr = SecretStr("")
    database_connect_timeout: int = Field(default=5, ge=1, le=60)
    hh_login_url: str = "https://hh.ru/account/login?role=applicant"
    hh_resumes_url: str = "https://hh.ru/applicant/resumes"
    hh_search_url: str = "https://hh.ru/search/vacancy"
    hh_browser_timeout_ms: int = Field(default=60_000, ge=1_000, le=120_000)
    yandex_ai_api_key: SecretStr = SecretStr("")
    yandex_ai_folder_id: str = ""
    yandex_ai_model: str = "aliceai-llm/latest"
    yandex_ai_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    yandex_ai_timeout_seconds: int = Field(default=120, ge=1, le=300)

    def browser_profile_dir(self, account_id: int) -> Path:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")
        return self.data_dir / "browser-profiles" / f"account-{account_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
