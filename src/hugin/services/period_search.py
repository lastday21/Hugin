from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from hugin.database import create_database
from hugin.database.models import ApplicationSettingsModel, DirectionVacancyModel, VacancyModel
from hugin.domain.automation import AutomationJobResult
from hugin.domain.hh_sync import HhSyncBlockedError, HhSyncRetryableError
from hugin.domain.search_coverage import PageCheckpoint, SearchCoverage, VariantCoverage
from hugin.domain.search_progress import fresh_search_today, search_time
from hugin.domain.time import as_utc, day_start_utc
from hugin.domain.vacancies import VacancyAvailability, VacancyData, VacancyUnavailableError
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_selection_gate import ApplicationSelectionGate
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.decision_evidence import fingerprint
from hugin.services.job_search import JobSearchSyncService
from hugin.services.search_cycle import BackgroundSearchCycle, SearchCycleBrowser
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE


class PeriodSearchCycle(BackgroundSearchCycle):
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
        signature_inputs = [
            account_id,
            search_query_id,
            [(t.query, t.area, t.filters) for t in tasks],
        ]
        signature = fingerprint(["period_search_v2", *signature_inputs])
        previous = (
            dict(progress or {}) if (progress or {}).get("cursor_signature") == signature else {}
        )
        old_signature = fingerprint(["period_search_v1", *signature_inputs])
        old_raw = (progress or {}).get("search_coverage")
        if (progress or {}).get("cursor_signature") == old_signature and isinstance(old_raw, str):
            old_coverage = SearchCoverage.model_validate_json(old_raw)
            previous = {
                "cursor_signature": signature,
                "search_coverage": SearchCoverage(
                    boundary=old_coverage.boundary,
                    started_at=old_coverage.started_at,
                    variants=[VariantCoverage() for _ in tasks],
                ).model_dump_json(),
                "incomplete_page_previous_coverage": old_raw,
                "incomplete_page_previous_signature": old_signature,
            }
        result: AutomationJobResult = {
            **previous,
            "cursor_signature": signature,
            "pages_loaded": 0,
            "details_loaded": 0,
            "details_failed": 0,
            "new_vacancies": 0,
            "new_links": 0,
            "queued": 0,
            "coverage_page_limit": None,
        }
        if not allowed():
            return {**result, "reason": "Поиск остановлен"}
        now = datetime.now(UTC)
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                options = session.get(ApplicationSettingsModel, 1)
                timezone_name = options.timezone_name if options else "UTC"
                repository = DirectionRepository(session)
                query = repository.get_query(search_query_id)
                direction = repository.get_for_account(account_id, query.direction_id)
                direction_id = direction.id
                interval = timedelta(minutes=query.schedule_minutes)
                gate = ApplicationSelectionGate(session)
                processes = BackgroundProcessService(session, account_id)
                running_pipeline = processes.enabled("applications") and processes.enabled(
                    "evaluation"
                )
                pending = gate.pending_details(account_id, now) if running_pipeline else {}
                blocking = gate.blocking_reason(account_id, now) if running_pipeline else None
                fresh_pending = gate.fresh_search_pending(account_id, now)
        finally:
            database.close()
        raw = previous.get("search_coverage")
        coverage = (
            SearchCoverage.model_validate_json(raw)
            if isinstance(raw, str)
            else SearchCoverage(
                boundary=now - MAX_VACANCY_AGE,
                started_at=now,
                variants=[VariantCoverage() for _ in tasks],
            )
        )
        if len(coverage.variants) != len(tasks):
            raise ValueError("Продолжение поиска не соответствует вариантам запроса")
        if "depth_variant_index" not in coverage.model_fields_set:
            coverage.depth_variant_index = (
                self._index(previous.get("variant_index"))
                if coverage.fresh_started_at is None
                else 0
            )
        if "fresh_variant_index" not in coverage.model_fields_set:
            coverage.fresh_variant_index = (
                self._index(previous.get("variant_index"))
                if coverage.fresh_started_at is not None
                else 0
            )
        completed_fresh = search_time(previous.get("fresh_search_at"))
        fresh_due = (
            prefer_fresh_search
            or not fresh_search_today(previous, now, timezone_name)
            or completed_fresh is None
            or now - completed_fresh >= interval
        )
        started = coverage.fresh_started_at
        if fresh_due and (started is None or as_utc(started) < day_start_utc(timezone_name, now)):
            if coverage.completed_at is not None:
                coverage.boundary = max(
                    now - MAX_VACANCY_AGE, as_utc(coverage.started_at) - timedelta(days=1)
                )
                coverage.started_at = now
                coverage.completed_at = None
                coverage.depth_variant_index = 0
                for variant in coverage.variants:
                    variant.depth = PageCheckpoint()
            coverage.fresh_started_at = now
            coverage.fresh_variant_index = 0
            for variant in coverage.variants:
                variant.fresh = PageCheckpoint(
                    anchor_id=variant.head_id,
                    anchor_at=variant.head_at,
                    seeking=variant.head_id is not None,
                    seek_from_start=True,
                )
                variant.new_head_id = None
                variant.new_head_at = None
            result.update(variant_index=0, next_step="search")
        fresh = coverage.fresh_started_at is not None
        if not fresh and running_pipeline:
            if pending and direction_id not in pending:
                return {**result, "reason": "Ожидает загрузки свежих вакансий других направлений"}
            if not pending and blocking and not fresh_pending:
                return {
                    **result,
                    "reason": "Ожидает оценки свежих вакансий перед продолжением поиска",
                }
        if browser.read_profile().external_id != external_id:
            raise RuntimeError("Аккаунт в браузере выбран неверно")
        if not fresh and (pending or result.get("next_step") in {"details", "backlog"}):
            loaded, failed, before, had_pending = self._load_details(
                direction_id,
                browser,
                allowed,
                self._index(previous.get("cursor_details_before")),
                pending.get(direction_id) if pending else None,
            )
            result.update(
                details_loaded=loaded,
                details_failed=failed,
                cursor_details_before=before,
                next_step="search",
                continuation=coverage.completed_at is None or had_pending,
                search_coverage=coverage.model_dump_json(),
            )
            if had_pending:
                return result
        start = (coverage.fresh_variant_index if fresh else coverage.depth_variant_index) % len(
            tasks
        )
        indices = ((start + offset) % len(tasks) for offset in range(len(tasks)))
        chosen = next(
            (
                i
                for i in indices
                if not (
                    coverage.variants[i].fresh if fresh else coverage.variants[i].depth
                ).complete
            ),
            None,
        )
        if chosen is None:
            return {
                **result,
                "continuation": False,
                "round_complete": True,
                "search_coverage": coverage.model_dump_json(),
            }
        variant = coverage.variants[chosen]
        cursor = variant.fresh if fresh else variant.depth
        if not fresh:
            cursor.begin_overlap()
        task = tasks[chosen]
        page = cursor.page
        found = browser.search_vacancies(
            task.query, area=task.area, filters=task.filters, page_number=page
        )
        if cursor.repeated_page(found.vacancies):
            raise HhSyncBlockedError(
                "HH_SEARCH_PAGINATION_STALLED",
                "hh.ru повторил ту же страницу для другого номера; "
                "продолжение сохранено, требуется проверка выдачи",
            )
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                ids = {v.hh_id for v in found.vacancies}
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
        finally:
            database.close()
        ordered = task.filters.get("order_by") == "publication_time"
        if fresh:
            if page == 0 and found.vacancies:
                head = found.vacancies[0]
                variant.new_head_id = head.hh_id
                variant.new_head_at = as_utc(head.published_at) if head.published_at else None
            if variant.head_id is None:
                cursor.complete = True
            else:
                cursor.observe(found.vacancies, coverage.boundary, ordered=ordered)
                cursor.complete = cursor.complete or not cursor.seeking
            if variant.depth.page == 0 and not variant.depth.complete:
                variant.depth.observe(found.vacancies, coverage.boundary, ordered=ordered)
            if cursor.complete:
                variant.head_id = variant.new_head_id
                variant.head_at = variant.new_head_at
            if all(v.fresh.complete for v in coverage.variants):
                result["fresh_search_at"] = now.isoformat()
                coverage.fresh_started_at = None
        else:
            cursor.observe(found.vacancies, coverage.boundary, ordered=ordered)
        finished = all(v.depth.complete for v in coverage.variants)
        if finished and coverage.completed_at is None:
            coverage.completed_at = now
        next_variant = (chosen + 1) % len(tasks)
        if fresh:
            coverage.fresh_variant_index = next_variant
        else:
            coverage.depth_variant_index = next_variant
        result.update(
            pages_loaded=1,
            new_vacancies=len(ids - existing),
            new_links=len(ids - linked),
            observed_query=task.query,
            observed_region=task.region_name,
            observed_page=page + 1,
            observed_found=found.found,
            observed_at=now.isoformat(),
            observed_search_kind="fresh" if fresh else "depth",
            coverage_exhausted=not found.vacancies,
            coverage_complete=finished,
            coverage_from_at=coverage.boundary.isoformat(),
            coverage_completed_at=coverage.completed_at.isoformat()
            if coverage.completed_at
            else None,
            coverage_next_page=None
            if variant.depth.complete
            else max(0, variant.depth.page - int(variant.depth.needs_overlap)) + 1,
            variant_index=next_variant,
            page_index=cursor.page,
            next_step="search" if coverage.fresh_started_at else "details",
            round_complete=finished,
            continuation=not finished or bool(found.vacancies),
            search_coverage=coverage.model_dump_json(),
        )
        result.pop("cursor_details_before", None)
        return result

    def _load_details(
        self,
        direction_id: int,
        browser: SearchCycleBrowser,
        allowed: Callable[[], bool],
        before: int,
        fresh_ids: set[int] | None,
    ) -> tuple[int, int, int, bool]:
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                statement = (
                    select(VacancyModel)
                    .join(DirectionVacancyModel)
                    .where(
                        DirectionVacancyModel.direction_id == direction_id,
                        VacancyModel.availability == VacancyAvailability.ACTIVE,
                        or_(
                            VacancyModel.details_fetched_at.is_(None),
                            VacancyModel.published_at > VacancyModel.details_fetched_at,
                        ),
                        or_(
                            VacancyModel.published_at.is_(None),
                            VacancyModel.published_at >= datetime.now(UTC) - MAX_VACANCY_AGE,
                        ),
                    )
                    .order_by(VacancyModel.id.desc())
                    .limit(min(3, self._detail_limit))
                )
                if fresh_ids is not None:
                    statement = statement.where(VacancyModel.id.in_(fresh_ids))
                rows = tuple(
                    session.scalars(
                        statement.where(VacancyModel.id < before) if before else statement
                    )
                )
                if not rows and before:
                    rows = tuple(session.scalars(statement))
            loaded = failed = 0
            for vacancy in rows:
                if not allowed():
                    break
                before = vacancy.id
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
                except (HhSyncBlockedError, HhSyncRetryableError):
                    raise
                except RuntimeError:
                    failed += 1
                    continue
                with database.sessions.begin() as session:
                    stored = VacancyRepository(session).upsert(details)
                    DirectionRepository(session).track_vacancy(direction_id, stored.id)
                loaded += 1
            return loaded, failed, before, bool(rows)
        finally:
            database.close()

    @staticmethod
    def _index(value: object) -> int:
        return value if type(value) is int and value >= 0 else 0
