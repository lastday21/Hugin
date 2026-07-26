from __future__ import annotations

from typing import Protocol

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.domain.automation import AutomationJobResult
from hugin.domain.hh import HhProfileData
from hugin.domain.vacancies import VacancyData, VacancySearchResult
from hugin.repositories.directions import AccountRepository, DirectionRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.career_directions import CareerDirectionService, VacancySearchTask
from hugin.services.hh_profile import HhProfileSyncService
from hugin.services.job_search import JobSearchSyncService
from hugin.services.vacancy_analysis import VacancyAnalysisService


class SearchCycleBrowser(Protocol):
    def read_profile(self) -> HhProfileData: ...

    def search_vacancies(
        self,
        query: str,
        *,
        area: str = "",
        filters: dict[str, object] | None = None,
        page_number: int = 0,
    ) -> VacancySearchResult: ...

    def read_vacancy_details(self, source_url: str) -> VacancyData: ...


class BackgroundSearchCycle:
    def __init__(self, settings: Settings, *, detail_limit: int = 20) -> None:
        if detail_limit < 1:
            raise ValueError("Ограничение подробной загрузки должно быть положительным")
        self._settings = settings
        self._detail_limit = detail_limit

    def run(
        self,
        *,
        account_id: int,
        search_query_id: int,
        browser: SearchCycleBrowser,
    ) -> AutomationJobResult:
        account_external_id, direction_name, tasks = self._tasks(account_id, search_query_id)
        profile = browser.read_profile()
        if profile.external_id != account_external_id:
            raise RuntimeError("Аккаунт в браузере выбран неверно")
        search_runs = tuple(
            (
                task,
                browser.search_vacancies(
                    task.query,
                    area=task.area,
                    filters=task.filters,
                    page_number=0,
                ),
            )
            for task in tasks
        )

        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                HhProfileSyncService(session).synchronize(profile)
                for task, result in search_runs:
                    JobSearchSyncService(session).synchronize(
                        account_external_id=profile.external_id,
                        direction_name=direction_name,
                        resume_title=None,
                        query=task.query,
                        area=task.area,
                        region=task.region_name,
                        search_query_id=task.search_query_id,
                        filters=task.filters,
                        vacancies=result.vacancies,
                    )
                pending = VacancyAnalysisService(session).pending(
                    account_external_id=profile.external_id,
                    direction_name=direction_name,
                    limit=self._detail_limit,
                )
        finally:
            database.close()

        detailed: list[VacancyData] = []
        failed_details = 0
        for vacancy in pending:
            try:
                detailed.append(browser.read_vacancy_details(vacancy.source_url))
            except RuntimeError:
                failed_details += 1

        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                analysis = VacancyAnalysisService(session)
                if detailed:
                    analysis.synchronize(
                        account_external_id=profile.external_id,
                        direction_name=direction_name,
                        vacancies=tuple(detailed),
                    )
                prepared = ApplicationAutomationService(session).prepare(
                    account_external_id=profile.external_id,
                    direction_name=direction_name,
                    include_stretch=True,
                )
        finally:
            database.close()

        unique_vacancies = {
            vacancy.hh_id for _task, result in search_runs for vacancy in result.vacancies
        }
        return {
            "search_variants": len(search_runs),
            "found": sum(result.found for _task, result in search_runs),
            "unique_vacancies": len(unique_vacancies),
            "details_loaded": len(detailed),
            "details_failed": failed_details,
            "queued": prepared.created,
        }

    def _tasks(
        self,
        account_id: int,
        search_query_id: int,
    ) -> tuple[str, str, tuple[VacancySearchTask, ...]]:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                directions = CareerDirectionService(session)
                account = AccountRepository(session).get(account_id)
                repository = DirectionRepository(session)
                query = repository.get_query(search_query_id)
                direction = repository.get_for_account(account_id, query.direction_id)
                if not account.external_id:
                    raise LookupError("Аккаунт hh.ru ещё не синхронизирован")
                tasks = tuple(
                    task
                    for task in directions.build_search_tasks(account_id, direction.name)
                    if task.search_query_id == search_query_id
                )
        finally:
            database.close()
        if not tasks:
            raise LookupError("Для фонового поиска не найдено активных вариантов")
        return account.external_id, direction.name, tasks
