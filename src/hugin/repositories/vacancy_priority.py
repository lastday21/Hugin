from __future__ import annotations

from typing import Any

from sqlalchemy import case, func
from sqlalchemy.sql.elements import ColumnElement

from hugin.database.models import ApplicationTaskModel, DirectionVacancyModel, VacancyModel


def vacancy_ordering() -> tuple[ColumnElement[Any], ...]:
    details = DirectionVacancyModel.rules_details
    tier = details["fit_tier"].as_string()
    return (
        case((tier == "1", 1), (tier == "2", 2), else_=3),
        func.coalesce(
            DirectionVacancyModel.rules_score, ApplicationTaskModel.priority_score, -1
        ).desc(),
        func.coalesce(details["location_priority"].as_float(), -1).desc(),
        func.coalesce(details["experience_priority"].as_float(), -1).desc(),
        VacancyModel.id.asc(),
    )
