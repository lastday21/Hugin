from datetime import UTC, datetime, timedelta, timezone

from hugin.domain.time import day_start_utc, local_timezone_name


def test_day_start_uses_configured_timezone_instead_of_process_timezone() -> None:
    selected_at = datetime(2026, 7, 31, 19, 1, tzinfo=UTC)

    assert day_start_utc("UTC+05:00", selected_at) == datetime(
        2026,
        7,
        31,
        19,
        tzinfo=UTC,
    )
    assert day_start_utc("UTC", selected_at) == datetime(
        2026,
        7,
        31,
        tzinfo=UTC,
    )


def test_local_timezone_name_uses_portable_offset_without_iana_key() -> None:
    windows_style_zone = timezone(timedelta(hours=5), "RTZ 4 (зима)")

    assert local_timezone_name(datetime(2026, 8, 1, tzinfo=windows_style_zone)) == "UTC+05:00"
