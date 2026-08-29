from __future__ import annotations

import os
from functools import lru_cache
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    if local_app_data := os.getenv("LOCALAPPDATA"):
        return Path(local_app_data) / "Hugin"
    return Path.home() / ".local" / "share" / "hugin"


def canonical_hh_account_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold()
    if hostname != "hh.ru" and not hostname.endswith(".hh.ru"):
        return value
    return urlunsplit((parsed.scheme, "hh.ru", parsed.path, parsed.query, parsed.fragment))


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
    desktop_api_url: str = "http://127.0.0.1:8010"
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
    hh_browser_source_ip: IPv4Address | None = None
    hh_background_search_pages: int = Field(default=3, ge=1, le=20)
    hh_background_detail_limit: int = Field(default=20, ge=1, le=50)
    telegram_bot_username: Literal["hugin_workbot"] = "hugin_workbot"
    notification_gateway_url: str = "http://127.0.0.1:8088"
    notification_gateway_key_file: Path | None = None
    notification_gateway_timeout_seconds: int = Field(default=15, ge=1, le=60)
    notification_gateway_connection_timeout_seconds: int = Field(
        default=120,
        ge=30,
        le=300,
    )
    yandex_ai_api_key: SecretStr = SecretStr("")
    yandex_ai_folder_id: str = ""
    yandex_ai_model: str = "aliceai-llm/latest"
    yandex_ai_router_model: str = "deepseek-v4-flash/latest"
    yandex_ai_router_reasoning_effort: Literal["low", "medium", "high"] = "low"
    yandex_ai_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    yandex_ai_host_ip: IPv4Address | None = None
    yandex_ai_source_ip: IPv4Address | None = None
    yandex_ai_timeout_seconds: int = Field(default=120, ge=1, le=300)
    codex_cli_path: Path | None = None
    codex_letter_model: str = "gpt-5.6-terra"
    codex_letter_reasoning_effort: Literal["low", "medium", "high"] = "low"
    codex_letter_timeout_seconds: int = Field(default=180, ge=30, le=300)
    codex_reply_requirement_model: str = "gpt-5.6-luna"
    codex_reply_requirement_timeout_seconds: int = Field(default=60, ge=30, le=300)

    @field_validator("hh_login_url", "hh_resumes_url")
    @classmethod
    def use_canonical_hh_account_host(cls, value: str) -> str:
        return canonical_hh_account_url(value)

    def browser_profile_dir(self, account_id: int) -> Path:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")
        return self.data_dir / "browser-profiles" / f"account-{account_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
