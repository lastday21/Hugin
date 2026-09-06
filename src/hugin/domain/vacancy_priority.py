from __future__ import annotations

from collections.abc import Mapping
from enum import IntEnum


class FitTier(IntEnum):
    DIRECT = 1
    RELATED = 2
    POSSIBLE = 3


FIT_TIER_LABELS = {
    FitTier.DIRECT: "1 · Прямое соответствие",
    FitTier.RELATED: "2 · Близкая работа",
    FitTier.POSSIBLE: "3 · Возможная работа",
}


def stored_fit_tier(details: Mapping[str, object]) -> FitTier | None:
    value = details.get("fit_tier")
    if type(value) is int and value in (1, 2, 3):
        return FitTier(value)
    return None


def vacancy_priority_key(
    details: Mapping[str, object], score: float | None, vacancy_id: int
) -> tuple[int, float, float, float, int]:
    def component(name: str) -> float:
        value = details.get(name)
        return float(value) if isinstance(value, int | float) else -1

    return (
        int(stored_fit_tier(details) or FitTier.POSSIBLE),
        -(score if score is not None else -1),
        -component("location_priority"),
        -component("experience_priority"),
        vacancy_id,
    )
