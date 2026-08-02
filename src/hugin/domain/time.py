import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC_OFFSET = re.compile(r"^UTC(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2})$")


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def local_day_start_utc(value: datetime | None = None) -> datetime:
    local_value = value or datetime.now().astimezone()
    if local_value.tzinfo is None:
        local_value = local_value.astimezone()
    return local_value.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def day_start_utc(timezone_name: str, value: datetime | None = None) -> datetime:
    selected_at = as_utc(value or datetime.now(UTC))
    local_value = selected_at.astimezone(timezone_by_name(timezone_name))
    return local_day_start_utc(local_value)


def local_timezone_name(value: datetime | None = None) -> str:
    local_value = value or datetime.now().astimezone()
    if local_value.tzinfo is None:
        local_value = local_value.astimezone()
    zone = local_value.tzinfo
    key = getattr(zone, "key", None)
    if key:
        return str(key)
    offset = local_value.utcoffset()
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"
    return str(local_value.tzname() or zone or "UTC")


def timezone_by_name(value: str) -> tzinfo:
    name = value.strip()
    if name in {"UTC", "Etc/UTC", "GMT"}:
        return UTC
    matched = UTC_OFFSET.fullmatch(name)
    if matched is not None:
        hours = int(matched.group("hours"))
        minutes = int(matched.group("minutes"))
        if hours > 23 or minutes > 59:
            return UTC
        offset = timedelta(hours=hours, minutes=minutes)
        if matched.group("sign") == "-":
            offset = -offset
        return timezone(offset, name)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return UTC
