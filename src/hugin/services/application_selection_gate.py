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
from hugin.domain.automation import AutomationJobState
from hugin.domain.time import as_utc, day_start_utc
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories.automation import search_configuration_key
from hugin.services.selection_status import SelectionRows, semantic_statuses
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE, RULES_VERSION


class ApplicationSelectionGate:
    def __init__(self, session: Session) -> None:
        self._session = session

    def blocking_reason(self, account_id: int, now: datetime | None = None) -> str | None:
        now = as_utc(now or datetime.now(UTC))
        if self.fresh_search_pending(account_id, now):
            return "Ожидает просмотра свежих страниц всех активных запросов"

        rows = self._current_rows(account_id, now)
        for tracked, vacancy, _ in rows:
            if self._needs_details(vacancy):
                return "Ожидает загрузки найденных вакансий перед выбором откликов"
            if tracked.rules_version != RULES_VERSION:
                return "Ожидает оценки найденных вакансий перед выбором откликов"
        if "PENDING" in semantic_statuses(self._session, account_id, rows).values():
            return "Ожидает оценки найденных вакансий перед выбором откликов"
        return None

    def pending_details(self, account_id: int, now: datetime | None = None) -> dict[int, set[int]]:
        selected_at = as_utc(now or datetime.now(UTC))
        pending: dict[int, set[int]] = {}
        for _, vacancy, direction in self._current_rows(account_id, selected_at):
            if self._needs_details(vacancy):
                pending.setdefault(direction.id, set()).add(vacancy.id)
        return pending

    @staticmethod
    def _needs_details(vacancy: VacancyModel) -> bool:
        return vacancy.details_fetched_at is None or (
            vacancy.published_at is not None
            and as_utc(vacancy.published_at) > as_utc(vacancy.details_fetched_at)
        )

    def _current_rows(self, account_id: int, now: datetime) -> SelectionRows:
        settings = self._session.get(ApplicationSettingsModel, 1)
        day_start = day_start_utc(settings.timezone_name if settings else "UTC", now)
        return self._session.execute(
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
                or_(
                    VacancyModel.created_at >= day_start,
                    VacancyModel.published_at >= day_start,
                ),
            )
            .order_by(DirectionVacancyModel.analyzed_at.asc().nullsfirst())
        ).all()

    def fresh_search_pending(
        self, account_id: int, now: datetime | None = None, *, due_only: bool = False
    ) -> bool:
        now = as_utc(now or datetime.now(UTC))
        settings = self._session.get(ApplicationSettingsModel, 1)
        day_start = day_start_utc(settings.timezone_name if settings else "UTC", now)
        if settings is not None and settings.search_enabled:
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
                if due_only and (
                    job is None
                    or job.state not in {AutomationJobState.WAITING, AutomationJobState.FAILED}
                    or job.next_run_at is None
                    or as_utc(job.next_run_at) > now
                ):
                    continue
                raw = job.last_result.get("fresh_search_at") if job is not None else None
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
                    or job.last_result.get("fresh_search_configuration")
                    != search_configuration_key(query)
                ):
                    return True
        return False
