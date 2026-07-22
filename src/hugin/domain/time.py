from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def local_day_start_utc(value: datetime | None = None) -> datetime:
    local_value = value or datetime.now().astimezone()
    if local_value.tzinfo is None:
        local_value = local_value.astimezone()
    return local_value.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def local_timezone_name(value: datetime | None = None) -> str:
    local_value = value or datetime.now().astimezone()
    if local_value.tzinfo is None:
        local_value = local_value.astimezone()
    zone = local_value.tzinfo
    key = getattr(zone, "key", None)
    return str(key or local_value.tzname() or zone or "UTC")
