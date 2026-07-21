from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from hugin.core.settings import Settings, default_data_dir, get_settings
from hugin.domain.time import local_day_start_utc


def test_default_api_is_local_only() -> None:
    settings = Settings()

    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.hh_browser_timeout_ms == 60_000
    assert settings.hh_apply_daily_limit == 25
    assert settings.hh_apply_delay_min_seconds == 30
    assert settings.hh_apply_delay_max_seconds == 60
    assert settings.data_dir.is_absolute()


def test_explicit_data_directory_is_preserved(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)

    assert settings.data_dir == tmp_path
    assert settings.browser_profile_dir(3) == tmp_path / "browser-profiles" / "account-3"


def test_browser_profile_requires_positive_account_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path).browser_profile_dir(0)


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


def test_apply_delay_range_is_validated() -> None:
    with pytest.raises(ValidationError, match="Максимальная задержка"):
        Settings(hh_apply_delay_min_seconds=30, hh_apply_delay_max_seconds=10)


def test_daily_limit_cannot_be_below_specification_minimum() -> None:
    with pytest.raises(ValidationError):
        Settings(hh_apply_daily_limit=24)


def test_local_day_start_is_converted_to_utc() -> None:
    local_zone = timezone(timedelta(hours=5))
    local_now = datetime(2026, 7, 21, 10, 30, tzinfo=local_zone)

    assert local_day_start_utc(local_now) == datetime(2026, 7, 20, 19, tzinfo=UTC)
