from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from hugin.core.settings import Settings, default_data_dir, get_settings
from hugin.domain.time import local_day_start_utc, local_timezone_name, timezone_by_name


def test_default_api_is_local_only() -> None:
    settings = Settings()

    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.hh_browser_timeout_ms == 60_000
    assert Settings.model_construct().hh_browser_source_ip is None
    assert settings.telegram_bot_username == "hugin_workbot"
    assert settings.notification_gateway_url == "http://127.0.0.1:8088"
    assert settings.notification_gateway_key_file is None
    assert settings.notification_gateway_timeout_seconds == 15
    assert settings.notification_gateway_connection_timeout_seconds == 120
    assert settings.yandex_ai_model == "aliceai-llm/latest"
    assert Settings.model_construct().yandex_ai_host_ip is None
    assert Settings.model_construct().yandex_ai_source_ip is None
    assert settings.data_dir.is_absolute()


def test_account_pages_use_canonical_hh_host_and_search_keeps_region() -> None:
    settings = Settings(
        hh_login_url="https://uchaly.hh.ru/account/login?role=applicant",
        hh_resumes_url="https://ufa.hh.ru/applicant/resumes",
        hh_search_url="https://uchaly.hh.ru/search/vacancy",
    )

    assert settings.hh_login_url == "https://hh.ru/account/login?role=applicant"
    assert settings.hh_resumes_url == "https://hh.ru/applicant/resumes"
    assert settings.hh_search_url == "https://uchaly.hh.ru/search/vacancy"


def test_browser_source_ip_is_validated() -> None:
    settings = Settings.model_validate({"hh_browser_source_ip": "192.168.0.18"})

    assert str(settings.hh_browser_source_ip) == "192.168.0.18"
    with pytest.raises(ValidationError):
        Settings.model_validate({"hh_browser_source_ip": "not-an-ip"})


def test_yandex_source_ip_is_validated() -> None:
    settings = Settings.model_validate(
        {
            "yandex_ai_host_ip": "158.160.54.160",
            "yandex_ai_source_ip": "192.168.0.18",
        }
    )

    assert str(settings.yandex_ai_host_ip) == "158.160.54.160"
    assert str(settings.yandex_ai_source_ip) == "192.168.0.18"
    with pytest.raises(ValidationError):
        Settings.model_validate({"yandex_ai_source_ip": "not-an-ip"})


def test_non_hh_account_urls_are_not_rewritten() -> None:
    settings = Settings(
        hh_login_url="http://localhost:9000/account/login",
        hh_resumes_url="http://localhost:9000/applicant/resumes",
    )

    assert settings.hh_login_url == "http://localhost:9000/account/login"
    assert settings.hh_resumes_url == "http://localhost:9000/applicant/resumes"


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


def test_local_day_start_is_converted_to_utc() -> None:
    local_zone = timezone(timedelta(hours=5))
    local_now = datetime(2026, 7, 21, 10, 30, tzinfo=local_zone)

    assert local_day_start_utc(local_now) == datetime(2026, 7, 20, 19, tzinfo=UTC)
    assert local_timezone_name(local_now) == "UTC+05:00"
    assert local_timezone_name(datetime(2026, 7, 21, 10, 30))


def test_timezone_name_supports_saved_offset_region_and_safe_fallback() -> None:
    value = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)

    assert value.astimezone(timezone_by_name("UTC+05:00")).hour == 21
    assert value.astimezone(timezone_by_name("UTC-03:30")).hour == 12
    assert timezone_by_name("Europe/Moscow").utcoffset(value) == timedelta(hours=3)
    assert timezone_by_name("UTC+99:99") is UTC
    assert timezone_by_name("missing/timezone") is UTC
