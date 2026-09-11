from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import or_, select

from hugin.database import create_database
from hugin.database.models import DirectionVacancyModel, VacancyModel
from hugin.domain.automation import AutomationJobResult
from hugin.domain.vacancies import VacancyAvailability, VacancyData, VacancyUnavailableError
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.decision_evidence import fingerprint
from hugin.services.job_search import JobSearchSyncService
from hugin.services.search_cycle import BackgroundSearchCycle, SearchCycleBrowser
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE


class IncrementalSearchCycle(BackgroundSearchCycle):
    def run(
        self,
        *,
        account_id: int,
        search_query_id: int,
        browser: SearchCycleBrowser,
        progress: AutomationJobResult | None = None,
        allowed: Callable[[], bool] = lambda: True,
        prefer_fresh_search: bool = False,
    ) -> AutomationJobResult:
        external_id, direction_name, tasks = self._tasks(account_id, search_query_id)
        if not tasks:
            return {"continuation": False, "reason": "Нет активных вариантов поиска"}
        signature = fingerprint([(t.query, t.area, t.filters) for t in tasks])
        previous = dict(progress or {})
        if previous.get("cursor_signature") != signature:
            previous = {}
        variant = self._index(previous.get("variant_index")) % len(tasks)
        page = min(self._index(previous.get("page_index")), self._page_limit - 1)
        stage = previous.get("next_step", "search")
        result: AutomationJobResult = {
            "cursor_signature": signature,
            "variant_index": variant,
            "page_index": page,
            "next_step": "search",
            "continuation": True,
            "pages_loaded": 0,
            "details_loaded": 0,
            "details_failed": 0,
            "new_vacancies": 0,
            "new_links": 0,
            "queued": 0,
        }
        # Последнее наблюдение выдачи отделено от счётчиков текущего хода.
        for key, value in previous.items():
            if key.startswith("observed_") or key in {
                "coverage_exhausted",
                "coverage_page_limit",
                "cursor_details_before",
            }:
                result[key] = value
        if not allowed():
            return {**result, "next_step": str(stage), "reason": "Поиск остановлен"}
        profile = browser.read_profile()
        if profile.external_id != external_id:
            raise RuntimeError("Аккаунт в браузере выбран неверно")
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                direction = DirectionRepository(session).get_by_account_and_name(
                    account_id, direction_name
                )
                if direction is None:
                    raise LookupError("Направление поиска не найдено")
                direction_id = direction.id
            if stage == "details":
                with database.sessions() as session:
                    candidates = (
                        select(VacancyModel)
                        .join(DirectionVacancyModel)
                        .where(
                            DirectionVacancyModel.direction_id == direction_id,
                            VacancyModel.details_fetched_at.is_(None),
                            VacancyModel.availability == VacancyAvailability.ACTIVE,
                            VacancyModel.duplicate_of_id.is_(None),
                            or_(
                                VacancyModel.published_at.is_(None),
                                VacancyModel.published_at >= datetime.now(UTC) - MAX_VACANCY_AGE,
                            ),
                        )
                        .order_by(VacancyModel.id.desc())
                        .limit(min(self._detail_limit, 3))
                    )
                    before = self._index(previous.get("cursor_details_before"))
                    pending = tuple(
                        session.scalars(
                            candidates.where(VacancyModel.id < before) if before else candidates
                        )
                    )
                    if not pending and before:
                        pending = tuple(session.scalars(candidates))
                loaded = failed = 0
                for vacancy in pending:
                    if not allowed():
                        break
                    result["cursor_details_before"] = vacancy.id
                    try:
                        details = browser.read_vacancy_details(vacancy.source_url)
                    except VacancyUnavailableError as error:
                        details = VacancyData(
                            vacancy.hh_id,
                            vacancy.title,
                            vacancy.source_url,
                            availability=error.availability,
                            details_fetched_at=datetime.now(UTC),
                        )
                    except RuntimeError:
                        failed += 1
                        continue
                    with database.sessions.begin() as session:
                        stored = VacancyRepository(session).upsert(details)
                        DirectionRepository(session).track_vacancy(direction_id, stored.id)
                    loaded += 1
                result.update(
                    details_loaded=loaded,
                    details_failed=failed,
                    continuation=previous.get("round_complete") is not True,
                )
                return result
            task = tasks[variant]
            found = browser.search_vacancies(
                task.query, area=task.area, filters=task.filters, page_number=page
            )
            with database.sessions.begin() as session:
                ids = tuple(v.hh_id for v in found.vacancies)
                existing = set(
                    session.scalars(select(VacancyModel.hh_id).where(VacancyModel.hh_id.in_(ids)))
                )
                linked = set(
                    session.scalars(
                        select(VacancyModel.hh_id)
                        .join(DirectionVacancyModel)
                        .where(
                            DirectionVacancyModel.direction_id == direction_id,
                            VacancyModel.hh_id.in_(ids),
                        )
                    )
                )
                JobSearchSyncService(session).synchronize(
                    account_external_id=external_id,
                    direction_name=direction_name,
                    resume_title=None,
                    query=task.query,
                    area=task.area,
                    region=task.region_name,
                    search_query_id=task.search_query_id,
                    filters=task.filters,
                    vacancies=found.vacancies,
                )
            exhausted = not found.vacancies
            variant_done = exhausted or page + 1 >= self._page_limit
            round_complete = variant_done and variant + 1 >= len(tasks)
            result.update(
                pages_loaded=1,
                new_vacancies=len(set(ids) - existing),
                new_links=len(set(ids) - linked),
                observed_query=task.query,
                observed_region=task.region_name,
                observed_page=page + 1,
                observed_found=found.found,
                observed_at=datetime.now(UTC).isoformat(),
                coverage_exhausted=exhausted,
                coverage_page_limit=self._page_limit,
                next_step="details",
                variant_index=(variant + 1) % len(tasks) if variant_done else variant,
                page_index=0 if variant_done else page + 1,
                round_complete=round_complete,
            )
            return result
        finally:
            database.close()

    @staticmethod
    def _index(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
