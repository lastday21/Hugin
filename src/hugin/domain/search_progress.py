from datetime import datetime

from hugin.domain.automation import AutomationJobResult
from hugin.domain.time import as_utc, day_start_utc


def search_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def fresh_search_today(result: AutomationJobResult, now: datetime, timezone_name: str) -> bool:
    completed = search_time(result.get("fresh_search_at"))
    return completed is not None and day_start_utc(timezone_name, now) <= completed <= now
