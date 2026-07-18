from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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

    @property
    def database_path(self) -> Path:
        return self.data_dir / "hugin.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
