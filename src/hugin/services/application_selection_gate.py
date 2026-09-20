from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationModel,
    ApplicationSettingsModel,
    AutomationJobModel,
    CareerDirectionModel,
    DirectionSearchQueryModel,
    DirectionVacancyModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationState
from hugin.domain.time import as_utc, day_start_utc
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories.automation import search_configuration_key
from hugin.services.selection_status import semantic_statuses
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE, RULES_VERSION


class ApplicationSelectionGate:
    def __init__(self, session: Session) -> None:
        self._session = session

    def blocking_reason(self, account_id: int, now: datetime | None = None) -> str | None:
        now = as_utc(now or datetime.now(UTC))
        settings = self._session.get(ApplicationSettingsModel, 1)
        if settings is not None and settings.search_enabled:
            day_start = day_start_utc(settings.timezone_name, now)
            queries = self._session.execute(
                select(DirectionSearchQueryModel, AutomationJobModel)
                .join(CareerDirectionModel)
                .outerjoin(
                    AutomationJobModel,
                    AutomationJobModel.search_query_id == DirectionSearchQueryModel.id,
                )
                .where(
                    CareerDirectionModel.account_id == account_id,
                    CareerDirectionModel.is_active.is_(True),
                    DirectionSearchQueryModel.is_active.is_(True),
                    DirectionSearchQueryModel.area == "",
                )
            )
            for query, job in queries:
                raw = job.last_result.get("completed_search_at") if job is not None else None
                try:
                    completed = (
                        as_utc(datetime.fromisoformat(raw)) if isinstance(raw, str) else None
                    )
                except ValueError:
                    completed = None
                if (
                    completed is None
                    or completed < day_start
                    or completed > now
                    or job.last_result.get("completed_search_configuration")
                    != search_configuration_key(query)
                ):
                    return "Ожидает завершения сегодняшнего поиска по всем активным запросам"

        rows = self._session.execute(
            select(DirectionVacancyModel, VacancyModel, CareerDirectionModel)
            .join(VacancyModel, VacancyModel.id == DirectionVacancyModel.vacancy_id)
            .join(
                CareerDirectionModel, CareerDirectionModel.id == DirectionVacancyModel.direction_id
            )
            .where(
                CareerDirectionModel.account_id == account_id,
                CareerDirectionModel.is_active.is_(True),
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                ~select(ApplicationModel.id)
                .where(
                    ApplicationModel.account_id == account_id,
                    ApplicationModel.vacancy_id == VacancyModel.id,
                    ApplicationModel.state.in_(
                        (
                            ApplicationState.APPLIED,
                            ApplicationState.VIEWED,
                            ApplicationState.INVITED,
                            ApplicationState.REJECTED,
                        )
                    ),
                )
                .exists(),
                or_(
                    VacancyModel.published_at.is_(None),
                    VacancyModel.published_at >= now - MAX_VACANCY_AGE,
                ),
            )
            .order_by(DirectionVacancyModel.analyzed_at.asc().nullsfirst())
        ).all()
        for tracked, vacancy, _ in rows:
            if vacancy.details_fetched_at is None:
                return "Ожидает загрузки найденных вакансий перед выбором откликов"
            if tracked.rules_version != RULES_VERSION:
                return "Ожидает оценки найденных вакансий перед выбором откликов"
        if "PENDING" in semantic_statuses(self._session, account_id, rows).values():
            return "Ожидает оценки найденных вакансий перед выбором откликов"
        return None
