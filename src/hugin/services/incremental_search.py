from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import or_, select

from hugin.database import create_database
from hugin.database.models import ApplicationSettingsModel, DirectionVacancyModel, VacancyModel
from hugin.domain.automation import AutomationJobResult
from hugin.domain.search_progress import fresh_search_today, search_time
from hugin.domain.time import day_start_utc
from hugin.domain.vacancies import VacancyAvailability, VacancyData, VacancyUnavailableError
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_selection_gate import ApplicationSelectionGate
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.decision_evidence import fingerprint
from hugin.services.job_search import JobSearchSyncService
from hugin.services.search_cycle import BackgroundSearchCycle, SearchCycleBrowser
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE


class IncrementalSearchCycle(BackgroundSearchCycle):
    _CURSOR_FIELDS = (
        "variant_index",
        "page_index",
        "next_step",
        "round_complete",
        "cursor_details_before",
        "cursor_backlog_before",
    )

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
        signature = fingerprint(
            ["fresh_pages_first_v1", [(t.query, t.area, t.filters) for t in tasks]]
        )
        previous = dict(progress or {})
        if previous.get("cursor_signature") != signature:
            previous = {}
        now = datetime.now(UTC)
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                options = session.get(ApplicationSettingsModel, 1)
                timezone_name = options.timezone_name if options is not None else "UTC"
        finally:
            database.close()
        fresh_sweep = not fresh_search_today(previous, now, timezone_name)
        started = search_time(previous.get("fresh_sweep_started_at"))
        if fresh_sweep and (
            started is None or not day_start_utc(timezone_name, now) <= started <= now
        ):
            if self._index(previous.get("page_index")) > 0:
                for key in self._CURSOR_FIELDS:
                    if key in previous:
                        previous[f"resume_{key}"] = previous[key]
            previous.update(
                variant_index=0,
                page_index=0,
                next_step="search",
                exhausted_mask=0,
                round_complete=False,
                continuation=True,
                fresh_sweep_started_at=now.isoformat(),
            )
            previous.pop("cursor_details_before", None)
            previous.pop("cursor_backlog_before", None)
        variant = self._index(previous.get("variant_index")) % len(tasks)
        page = min(self._index(previous.get("page_index")), self._page_limit - 1)
        stage = previous.get("next_step", "search")
        if (
            stage == "search"
            and previous.get("continuation") is False
            and previous.get("backlog_processed") is not True
        ):
            stage = "backlog"
        exhausted_mask = self._index(previous.get("exhausted_mask")) & ((1 << len(tasks)) - 1)
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
            "exhausted_mask": exhausted_mask,
        }
        # Последнее наблюдение выдачи отделено от счётчиков текущего хода.
        for key, value in previous.items():
            if key.startswith(("observed_", "resume_")) or key in {
                "coverage_exhausted",
                "coverage_page_limit",
                "cursor_details_before",
                "cursor_backlog_before",
                "fresh_search_at",
                "fresh_search_configuration",
                "fresh_sweep_started_at",
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
                fresh_ids: set[int] | None = None
                processes = BackgroundProcessService(session, account_id)
                gate = ApplicationSelectionGate(session)
                if (
                    not fresh_sweep
                    and processes.enabled("applications")
                    and processes.enabled("evaluation")
                    and not gate.fresh_search_pending(account_id, now)
                ):
                    for key in ("round_complete", "backlog_processed"):
                        if key in previous:
                            result[key] = previous[key]
                    current_details = gate.pending_details(account_id, now)
                    if current_details:
                        fresh_ids = current_details.get(direction_id)
                        if not fresh_ids:
                            return {
                                **result,
                                "next_step": str(stage),
                                "reason": "Ожидает загрузки свежих вакансий других направлений",
                            }
                        stage = "details"
                    elif gate.blocking_reason(account_id, now):
                        return {
                            **result,
                            "next_step": str(stage),
                            "reason": "Ожидает оценки свежих вакансий перед продолжением поиска",
                        }
            if stage in {"details", "backlog"}:
                with database.sessions() as session:
                    candidates = (
                        select(VacancyModel)
                        .join(DirectionVacancyModel)
                        .where(
                            DirectionVacancyModel.direction_id == direction_id,
                            or_(
                                VacancyModel.details_fetched_at.is_(None),
                                VacancyModel.published_at > VacancyModel.details_fetched_at,
                            ),
                            VacancyModel.availability == VacancyAvailability.ACTIVE,
                            or_(
                                VacancyModel.published_at.is_(None),
                                VacancyModel.published_at >= datetime.now(UTC) - MAX_VACANCY_AGE,
                            ),
                        )
                        .order_by(VacancyModel.id.desc())
                        .limit(min(self._detail_limit, 3))
                    )
                    if fresh_ids is not None:
                        candidates = candidates.where(VacancyModel.id.in_(fresh_ids))
                    cursor_key = (
                        "cursor_backlog_before" if stage == "backlog" else "cursor_details_before"
                    )
                    before = self._index(previous.get(cursor_key))
                    pending = tuple(
                        session.scalars(
                            candidates.where(VacancyModel.id < before) if before else candidates
                        )
                    )
                    if not pending and before and stage == "details":
                        pending = tuple(session.scalars(candidates))
                if stage == "backlog" and not pending:
                    stage = "search"
                    result.pop("cursor_backlog_before", None)
                loaded = failed = 0
                for vacancy in pending:
                    if not allowed():
                        break
                    result[cursor_key] = vacancy.id
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
                if stage != "search":
                    result.update(
                        details_loaded=loaded,
                        details_failed=failed,
                        continuation=fresh_ids is not None
                        or stage == "backlog"
                        or previous.get("round_complete") is not True,
                    )
                    if stage == "backlog":
                        result.update(backlog_processed=True, next_step="search")
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
            if exhausted:
                exhausted_mask |= 1 << variant
            next_slot = page * len(tasks) + variant + 1
            while next_slot < self._page_limit * len(tasks):
                next_variant = next_slot % len(tasks)
                if not exhausted_mask & (1 << next_variant):
                    break
                next_slot += 1
            round_complete = next_slot >= self._page_limit * len(tasks)
            next_page, next_variant = (0, 0) if round_complete else divmod(next_slot, len(tasks))
            first_pages_complete = page == 0 and (round_complete or next_page > 0)
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
                next_step="search"
                if exhausted or (fresh_sweep and not first_pages_complete)
                else "details",
                continuation=not round_complete if exhausted else True,
                variant_index=next_variant,
                page_index=next_page,
                exhausted_mask=0 if round_complete else exhausted_mask,
                round_complete=round_complete,
            )
            result.pop("cursor_details_before", None)
            if first_pages_complete:
                result["fresh_search_at"] = (
                    str(previous["fresh_sweep_started_at"])
                    if fresh_sweep
                    else datetime.now(UTC).isoformat()
                )
                result["fresh_sweep_started_at"] = None
                if "resume_page_index" in result:
                    for key in self._CURSOR_FIELDS:
                        if f"resume_{key}" in result:
                            result[key] = result.pop(f"resume_{key}")
                    result.update(continuation=True, exhausted_mask=exhausted_mask)
            return result
        finally:
            database.close()

    @staticmethod
    def _index(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
