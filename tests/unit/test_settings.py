from pathlib import Path

import pytest
from pydantic import ValidationError

from hugin.core.settings import Settings, default_data_dir, get_settings


def test_default_api_is_local_only() -> None:
    settings = Settings()

    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.data_dir.is_absolute()


def test_explicit_data_directory_is_preserved(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)

    assert settings.data_dir == tmp_path


def test_database_settings_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUGIN_DATABASE_HOST", "database.internal")
    monkeypatch.setenv("HUGIN_DATABASE_PORT", "5544")
    monkeypatch.setenv("HUGIN_DATABASE_NAME", "hugin_test")
    monkeypatch.setenv("HUGIN_DATABASE_USER", "hugin_user")
    monkeypatch.setenv("HUGIN_DATABASE_PASSWORD", "secret")

    settings = Settings()

    assert settings.database_host == "database.internal"
    assert settings.database_port == 5544
    assert settings.database_name == "hugin_test"
    assert settings.database_user == "hugin_user"
    assert settings.database_password.get_secret_value() == "secret"


def test_default_data_directory_uses_local_app_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_data_dir() == tmp_path / "Hugin"


def test_default_data_directory_has_portable_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert default_data_dir().parts[-3:] == (".local", "share", "hugin")


def test_cached_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUGIN_API_PORT", "8123")
    get_settings.cache_clear()

    try:
        assert get_settings().api_port == 8123
    finally:
        get_settings.cache_clear()


def test_port_outside_tcp_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(api_port=65536)
