from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Self

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    AutomationJobModel,
    CareerDirectionModel,
    DirectionSearchQueryModel,
    DirectionVacancyModel,
    VacancyModel,
)
from hugin.domain import SearchRegion, VacancyData
from hugin.domain.automation import AutomationJobResult
from hugin.domain.hh_sync import HhSyncBlockedError, HhSyncRetryableError
from hugin.domain.search_coverage import SearchCoverage
from hugin.domain.vacancies import VacancySearchResult
from hugin.repositories.automation import search_configuration_key
from hugin.services.automation import AutomationSchedulerService
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.decision_evidence import fingerprint
from hugin.services.period_search import PeriodSearchCycle
from hugin.services.vacancy_analysis import RULES_VERSION
from hugin.workers.hh_search import HhSearchJobHandler
from tests.unit.test_incremental_search import Browser, seed

pytestmark = pytest.mark.integration


class PeriodBrowser(Browser):
    def __init__(self) -> None:
        super().__init__()
        now = datetime.now(UTC)
        self.vacancies = [
            VacancyData(
                str(100 + i),
                "Python",
                f"https://hh.ru/vacancy/{100 + i}",
                published_at=now - timedelta(days=i // 4),
            )
            for i in range(24)
        ]

    def search_vacancies(
        self,
        query: str,
        *,
        area: str = "",
        filters: dict[str, object] | None = None,
        page_number: int = 0,
    ) -> VacancySearchResult:
        self.pages.append((query, area, page_number))
        if self.fail:
            raise RuntimeError("network failure")
        return VacancySearchResult(
            found=len(self.vacancies),
            vacancies=tuple(self.vacancies[page_number * 4 : (page_number + 1) * 4]),
        )


def test_native_search_continues_after_third_page_across_restarts(settings: Settings) -> None:
    account, query = seed(settings)
    browser = PeriodBrowser()
    progress = None
    for _ in range(20):
        cycle = HhSearchJobHandler(settings, account_id=account, incremental=True)._cycle
        assert isinstance(cycle, PeriodSearchCycle)
        progress = cycle.run(
            account_id=account, search_query_id=query, browser=browser, progress=progress
        )
    assert 3 in [page for _, _, page in browser.pages]
    assert "https://hh.ru/vacancy/112" in browser.details


def coverage(progress: AutomationJobResult) -> SearchCoverage:
    raw = progress["search_coverage"]
    assert isinstance(raw, str)
    return SearchCoverage.model_validate_json(raw)


def test_deleted_pages_relocate_boundary_before_finishing_coverage(settings: Settings) -> None:
    account, query = seed(settings)
    browser = PeriodBrowser()
    cycle = PeriodSearchCycle(settings)
    progress: AutomationJobResult = {}
    for _ in range(60):
        progress = cycle.run(
            account_id=account, search_query_id=query, browser=browser, progress=progress
        )
        cursor = coverage(progress).variants[0].depth
        if cursor.page == 4 and cursor.needs_overlap:
            break
    assert cursor.page == 4
    browser.vacancies = browser.vacancies[16:]
    for _ in range(20):
        progress = cycle.run(
            account_id=account, search_query_id=query, browser=browser, progress=progress
        )
    with create_database(settings).sessions() as session:
        ids = set(session.scalars(select(VacancyModel.hh_id)))
    assert {str(i) for i in range(116, 124)} <= ids


def test_fresh_publication_flood_is_read_without_resetting_depth_or_duplicates(
    settings: Settings,
) -> None:
    account, query = seed(settings)
    browser = PeriodBrowser()
    cycle = PeriodSearchCycle(settings)
    progress = cycle.run(account_id=account, search_query_id=query, browser=browser)
    saved_depth = coverage(progress).variants[0].depth.model_dump()
    new = [
        VacancyData(str(i), "Python", f"https://hh.ru/vacancy/{i}", published_at=datetime.now(UTC))
        for i in range(1000, 1028)
    ]
    browser.vacancies = new + browser.vacancies
    progress["fresh_search_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    for _ in range(12):
        progress = PeriodSearchCycle(settings).run(
            account_id=account, search_query_id=query, browser=browser, progress=progress
        )
        if coverage(progress).fresh_started_at is None:
            break
    state = coverage(progress)
    assert state.fresh_started_at is None
    assert state.variants[0].depth.model_dump() == saved_depth
    with create_database(settings).sessions() as session:
        ids = list(session.scalars(select(VacancyModel.hh_id)))
    assert len(ids) == len(set(ids))
    assert {str(i) for i in range(1000, 1028)} <= set(ids)


def test_periodic_fresh_checks_preserve_depth_region_rotation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, query = seed(
        settings,
        regions=(
            SearchRegion("1", "Москва"),
            SearchRegion("2", "СПб"),
            SearchRegion("3", "Екатеринбург"),
        ),
    )
    current = datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(current.timestamp(), tz=tz or UTC)

    monkeypatch.setattr("hugin.services.period_search.datetime", Clock)
    browser = PeriodBrowser()
    browser.vacancies = [
        replace(v, description="Python API", details_fetched_at=datetime.now(UTC))
        for v in browser.vacancies
    ]
    progress: AutomationJobResult = {}

    def step() -> AutomationJobResult:
        return PeriodSearchCycle(settings).run(
            account_id=account, search_query_id=query, browser=browser, progress=deepcopy(progress)
        )

    for _ in range(3):
        progress = step()
    assert coverage(progress).fresh_started_at is None
    depth_regions = []
    for _ in range(3):
        progress = step()
        assert progress["observed_search_kind"] == "depth"
        depth_regions.append(browser.pages[-1][1])
        current += timedelta(hours=3)
        for _ in range(3):
            progress = step()
            assert progress["observed_search_kind"] == "fresh"
        assert coverage(progress).fresh_started_at is None
    assert depth_regions == ["1", "2", "3"]


def test_unknown_publication_date_does_not_close_the_period(settings: Settings) -> None:
    account, query = seed(settings)
    browser = PeriodBrowser()
    expired = datetime.now(UTC) - timedelta(days=31)
    browser.vacancies[4:8] = [replace(v, published_at=expired) for v in browser.vacancies[4:8]]
    browser.vacancies[5] = replace(browser.vacancies[5], published_at=None)
    progress: AutomationJobResult = {}
    for _ in range(20):
        progress = PeriodSearchCycle(settings).run(
            account_id=account, search_query_id=query, browser=browser, progress=progress
        )
    assert 3 in [page for _, _, page in browser.pages]


def test_page_failure_and_user_stop_preserve_checkpoint(settings: Settings) -> None:
    account, query = seed(settings)
    browser = PeriodBrowser()
    cycle = PeriodSearchCycle(settings)
    progress = cycle.run(account_id=account, search_query_id=query, browser=browser)
    previous = deepcopy(progress)
    stopped = cycle.run(
        account_id=account,
        search_query_id=query,
        browser=browser,
        progress=progress,
        allowed=lambda: False,
    )
    assert stopped["search_coverage"] == previous["search_coverage"]
    assert len(browser.pages) == 1
    progress["next_step"] = "search"
    previous = deepcopy(progress)
    browser.fail = True
    with pytest.raises(RuntimeError, match="network failure"):
        cycle.run(account_id=account, search_query_id=query, browser=browser, progress=progress)
    assert progress == previous
    browser.fail = False
    resumed = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=progress
    )
    assert browser.pages[-1] == browser.pages[-2]
    assert resumed["pages_loaded"] == 1


def test_period_boundary_stops_only_ordered_fully_dated_pages(settings: Settings) -> None:
    account, query = seed(settings)
    browser = PeriodBrowser()
    expired = datetime.now(UTC) - timedelta(days=31)
    browser.vacancies[4:] = [replace(v, published_at=expired) for v in browser.vacancies[4:]]
    progress: AutomationJobResult = {}
    for _ in range(10):
        progress = PeriodSearchCycle(settings).run(
            account_id=account, search_query_id=query, browser=browser, progress=progress
        )
        if progress.get("coverage_complete"):
            break
    assert coverage(progress).completed_at is not None
    assert max(page for _, _, page in browser.pages) == 1


def test_region_checkpoints_are_independent_and_changed_filters_start_fresh(
    settings: Settings,
) -> None:
    account, query = seed(settings, regions=(SearchRegion("1", "Москва"), SearchRegion("2", "СПб")))
    browser = PeriodBrowser()
    cycle = PeriodSearchCycle(settings)
    first = cycle.run(account_id=account, search_query_id=query, browser=browser)
    assert [v.depth.page for v in coverage(first).variants] == [1, 0]
    first["search_coverage"] = coverage(first).model_dump_json(
        exclude={"depth_variant_index", "fresh_variant_index"}
    )
    second = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=first)
    assert [v.depth.page for v in coverage(second).variants] == [1, 1]
    assert [area for _, area, _ in browser.pages] == ["1", "2"]
    second["next_step"] = "search"
    legacy = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=second)
    assert browser.pages[-1][1] == "1"
    legacy["search_coverage"] = coverage(legacy).model_dump_json(
        exclude={"depth_variant_index", "fresh_variant_index"}
    )
    legacy["next_step"] = "search"
    resumed = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=legacy)
    assert browser.pages[-1][1] == "2"
    assert [v.depth.page for v in coverage(resumed).variants] == [1, 1]
    with create_database(settings).sessions.begin() as session:
        row = session.get(DirectionSearchQueryModel, query)
        assert row is not None
        row.filters = {**row.filters, "text": "Changed"}
    third = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=resumed)
    assert third["cursor_signature"] != second["cursor_signature"]
    assert [v.depth.page for v in coverage(third).variants] == [1, 0]


def test_incomplete_card_contract_restarts_coverage_without_losing_original_period(
    settings: Settings,
) -> None:
    account, query = seed(settings, regions=(SearchRegion("1", "Москва"), SearchRegion("2", "СПб")))
    browser = PeriodBrowser()
    cycle = PeriodSearchCycle(settings)
    previous = cycle.run(account_id=account, search_query_id=query, browser=browser)
    old = coverage(previous)
    old.boundary -= timedelta(days=1)
    old.completed_at = datetime.now(UTC)
    for variant in old.variants:
        variant.depth.page = 4
        variant.depth.complete = True
        variant.fresh.complete = True
    raw = old.model_dump_json()
    previous["search_coverage"] = raw
    _, _, tasks = cycle._tasks(account, query)
    previous["cursor_signature"] = fingerprint(
        [
            "period_search_v1",
            account,
            query,
            [(t.query, t.area, t.filters) for t in tasks],
        ]
    )
    recovered = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=previous
    )
    state = coverage(recovered)
    assert browser.pages[-1][2] == 0
    assert state.boundary == old.boundary
    assert state.started_at == old.started_at
    assert state.completed_at is None
    assert state.variants[1].depth.page == 0
    assert not state.variants[1].depth.complete
    assert recovered["incomplete_page_previous_coverage"] == raw
    assert recovered["cursor_signature"] != previous["cursor_signature"]
    resumed = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=recovered
    )
    assert resumed["incomplete_page_previous_coverage"] == raw
    assert coverage(resumed).boundary == old.boundary
    assert browser.pages[-1][1:] == ("2", 0)


def test_repeated_page_for_another_number_cannot_loop_forever(settings: Settings) -> None:
    class RepeatingBrowser(PeriodBrowser):
        def search_vacancies(
            self,
            query: str,
            *,
            area: str = "",
            filters: dict[str, object] | None = None,
            page_number: int = 0,
        ) -> VacancySearchResult:
            return super().search_vacancies(query, area=area, filters=filters, page_number=0)

    account, query = seed(settings)
    browser = RepeatingBrowser()
    progress: AutomationJobResult = {}
    with pytest.raises(HhSyncBlockedError, match="повторил ту же страницу"):
        for _ in range(10):
            progress = PeriodSearchCycle(settings).run(
                account_id=account, search_query_id=query, browser=browser, progress=progress
            )


def test_pipeline_drains_details_and_current_selection_before_depth(settings: Settings) -> None:
    account, query = seed(settings)
    browser = PeriodBrowser()
    cycle = PeriodSearchCycle(settings)
    progress = cycle.run(account_id=account, search_query_id=query, browser=browser)
    with create_database(settings).sessions.begin() as session:
        stored_query = session.get(DirectionSearchQueryModel, query)
        assert stored_query is not None
        direction = session.get(CareerDirectionModel, stored_query.direction_id)
        assert direction is not None
        direction.scoring_config = {}
        service = BackgroundProcessService(session, account)
        service.set_enabled("evaluation", True)
        service.set_enabled("applications", True)
        AutomationSchedulerService(session).ensure_configured_jobs(account)
        job = session.get(AutomationJobModel, f"search:{query}")
        assert job is not None
        job.last_result = {
            **progress,
            "fresh_search_configuration": search_configuration_key(stored_query),
        }
    for expected in (3, 1):
        progress = cycle.run(
            account_id=account, search_query_id=query, browser=browser, progress=progress
        )
        assert progress["details_loaded"] == expected and len(browser.pages) == 1
    waiting = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=progress
    )
    assert len(browser.pages) == 1 and "Ожидает оценки" in str(waiting["reason"])
    assert waiting["search_coverage"] == progress["search_coverage"]
    with create_database(settings).sessions.begin() as session:
        for row in session.scalars(select(DirectionVacancyModel)):
            row.rules_version = RULES_VERSION
    resumed = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=waiting
    )
    assert resumed["pages_loaded"] == 1


@pytest.mark.parametrize("captcha", [False, True], ids=["closed_browser", "captcha"])
def test_browser_failure_during_details_keeps_reason_and_checkpoint(
    settings: Settings,
    captcha: bool,
) -> None:
    class ClosedBrowser(PeriodBrowser):
        def read_vacancy_details(self, source_url: str) -> VacancyData:
            if captcha:
                raise HhSyncBlockedError("CAPTCHA_REQUIRED", "hh.ru запросил проверку")
            raise HhSyncRetryableError(
                "HH_BROWSER_CLOSED", "Браузер закрыт", retry_after_seconds=60
            )

    account, query = seed(settings)
    browser = ClosedBrowser()
    cycle = PeriodSearchCycle(settings)
    progress = cycle.run(account_id=account, search_query_id=query, browser=browser)
    previous = deepcopy(progress)
    with pytest.raises((HhSyncBlockedError, HhSyncRetryableError)) as raised:
        cycle.run(account_id=account, search_query_id=query, browser=browser, progress=progress)
    assert isinstance(raised.value, (HhSyncBlockedError, HhSyncRetryableError))
    assert raised.value.code == ("CAPTCHA_REQUIRED" if captcha else "HH_BROWSER_CLOSED")
    if isinstance(raised.value, HhSyncRetryableError):
        assert raised.value.retry_after_seconds == 60
    assert progress == previous and len(browser.pages) == 1
