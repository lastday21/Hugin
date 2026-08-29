from __future__ import annotations

from typing import Protocol

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.domain.automation import AutomationJobResult
from hugin.domain.hh import HhProfileData
from hugin.domain.vacancies import (
    VacancyData,
    VacancyRecord,
    VacancySearchResult,
    VacancyUnavailableError,
)
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
    _DETAIL_CHUNK_SIZE = 1

    def __init__(
        self,
        settings: Settings,
        *,
        page_limit: int = 3,
        detail_limit: int = 20,
    ) -> None:
        if page_limit < 1:
            raise ValueError("Количество страниц поиска должно быть положительным")
        if detail_limit < 1:
            raise ValueError("Ограничение подробной загрузки должно быть положительным")
        self._settings = settings
        self._page_limit = page_limit
        self._detail_limit = detail_limit

    def run(
        self,
        *,
        account_id: int,
        search_query_id: int,
        browser: SearchCycleBrowser,
        prefer_fresh_search: bool = False,
    ) -> AutomationJobResult:
        account_external_id, direction_name, tasks = self._tasks(account_id, search_query_id)
        profile = browser.read_profile()
        if profile.external_id != account_external_id:
            raise RuntimeError("Аккаунт в браузере выбран неверно")

        pending = self._pending(
            account_external_id=profile.external_id,
            direction_name=direction_name,
        )
        if pending and not prefer_fresh_search:
            details_loaded, details_failed, queued = self._process_pending(
                account_external_id=profile.external_id,
                direction_name=direction_name,
                browser=browser,
                pending=pending,
            )
            return {
                "search_variants": 0,
                "pages_loaded": 0,
                "found": 0,
                "unique_vacancies": 0,
                "details_loaded": details_loaded,
                "details_failed": details_failed,
                "queued": queued,
                "backlog_processed": True,
            }

        search_runs: list[tuple[VacancySearchTask, tuple[VacancySearchResult, ...]]] = []
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                HhProfileSyncService(session).synchronize(profile)
            for task in tasks:
                pages: list[VacancySearchResult] = []
                for page_number in range(self._page_limit):
                    result = browser.search_vacancies(
                        task.query,
                        area=task.area,
                        filters=task.filters,
                        page_number=page_number,
                    )
                    if not result.vacancies:
                        break
                    pages.append(result)
                    with database.sessions.begin() as session:
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
                search_runs.append((task, tuple(pages)))
        finally:
            database.close()

        pending = self._pending(
            account_external_id=profile.external_id,
            direction_name=direction_name,
        )
        details_loaded, failed_details, queued = self._process_pending(
            account_external_id=profile.external_id,
            direction_name=direction_name,
            browser=browser,
            pending=pending,
        )

        unique_vacancies = {
            vacancy.hh_id
            for _task, pages in search_runs
            for result in pages
            for vacancy in result.vacancies
        }
        return {
            "search_variants": len(search_runs),
            "pages_loaded": sum(len(pages) for _task, pages in search_runs),
            "found": sum(pages[0].found for _task, pages in search_runs if pages),
            "unique_vacancies": len(unique_vacancies),
            "details_loaded": details_loaded,
            "details_failed": failed_details,
            "queued": queued,
            "backlog_processed": False,
        }

    def _pending(
        self,
        *,
        account_external_id: str,
        direction_name: str,
    ) -> tuple[VacancyRecord, ...]:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                return VacancyAnalysisService(session).pending(
                    account_external_id=account_external_id,
                    direction_name=direction_name,
                    limit=self._detail_limit,
                )
        finally:
            database.close()

    def _process_pending(
        self,
        *,
        account_external_id: str,
        direction_name: str,
        browser: SearchCycleBrowser,
        pending: tuple[VacancyRecord, ...],
    ) -> tuple[int, int, int]:
        details_loaded = 0
        details_failed = 0
        queued = 0
        if not pending:
            queued = self._synchronize_and_prepare(
                account_external_id=account_external_id,
                direction_name=direction_name,
                detailed=(),
            )
            return details_loaded, details_failed, queued
        for offset in range(0, len(pending), self._DETAIL_CHUNK_SIZE):
            detailed: list[VacancyData] = []
            for vacancy in pending[offset : offset + self._DETAIL_CHUNK_SIZE]:
                try:
                    detailed.append(browser.read_vacancy_details(vacancy.source_url))
                except VacancyUnavailableError as error:
                    detailed.append(
                        VacancyData(
                            hh_id=vacancy.hh_id,
                            title=vacancy.title,
                            source_url=vacancy.source_url,
                            employer_name=vacancy.employer_name,
                            published_at=vacancy.published_at,
                            region=vacancy.region,
                            availability=error.availability,
                        )
                    )
                except RuntimeError:
                    details_failed += 1
            details_loaded += len(detailed)
            queued += self._synchronize_and_prepare(
                account_external_id=account_external_id,
                direction_name=direction_name,
                detailed=tuple(detailed),
            )
            if queued:
                break
        return details_loaded, details_failed, queued

    def _synchronize_and_prepare(
        self,
        *,
        account_external_id: str,
        direction_name: str,
        detailed: tuple[VacancyData, ...],
    ) -> int:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                analysis = VacancyAnalysisService(session)
                vacancy_ids: tuple[int, ...] = ()
                if detailed:
                    analyzed = analysis.synchronize(
                        account_external_id=account_external_id,
                        direction_name=direction_name,
                        vacancies=detailed,
                    )
                    vacancy_ids = tuple(item.vacancy.id for item in analyzed)
                automation = ApplicationAutomationService(session)
                include_stretch = automation.stretch_automation_enabled()
                if vacancy_ids:
                    return automation.prepare_vacancies(
                        account_external_id=account_external_id,
                        vacancy_ids=vacancy_ids,
                        include_stretch=include_stretch,
                    )
                prepared = automation.prepare(
                    account_external_id=account_external_id,
                    direction_name=direction_name,
                    include_stretch=include_stretch,
                )
                return prepared.created
        finally:
            database.close()

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
