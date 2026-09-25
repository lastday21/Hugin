from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationModel,
    ApplicationSettingsModel,
    CandidateProfileModel,
    DirectionSearchQueryModel,
    VacancyModel,
)
from hugin.domain import SearchRegion, VacancyData
from hugin.domain.automation import AutomationJobKind
from hugin.domain.hh import HhProfileData, HhResumeData
from hugin.domain.vacancies import VacancySearchResult
from hugin.repositories import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.services.application_selection_gate import ApplicationSelectionGate
from hugin.services.automation import AutomationSchedulerService
from hugin.services.incremental_search import IncrementalSearchCycle

pytestmark = pytest.mark.integration


class Browser:
    def __init__(self) -> None:
        self.pages: list[tuple[str, str, int]] = []
        self.details: list[str] = []
        self.fail = False
        self.empty = False
        self.failed_detail_ids: set[str] = set()

    def read_profile(self) -> HhProfileData:
        return HhProfileData(
            external_id="incremental", label="Test", resumes=(HhResumeData("resume", "Python"),)
        )

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
        if self.empty:
            return VacancySearchResult(found=0, vacancies=())
        return VacancySearchResult(
            found=4,
            vacancies=tuple(
                VacancyData(
                    str(100 + page_number * 4 + i),
                    "Python",
                    f"https://hh.ru/vacancy/{100 + page_number * 4 + i}",
                    published_at=datetime.now(UTC),
                )
                for i in range(4)
            ),
        )

    def read_vacancy_details(self, source_url: str) -> VacancyData:
        self.details.append(source_url)
        if source_url.rsplit("/", 1)[-1] in self.failed_detail_ids:
            raise RuntimeError("description unavailable")
        return VacancyData(
            source_url.rsplit("/", 1)[-1],
            "Python",
            source_url,
            description="Develop Python API",
            details_fetched_at=datetime.now(UTC),
        )


def seed(settings: Settings, *, regions: tuple[SearchRegion, ...] | None = None) -> tuple[int, int]:
    with create_database(settings).sessions.begin() as s:
        a = AccountRepository(s).create("Test", "incremental")
        r = ResumeRepository(s).upsert(a.id, "resume", "Python")
        s.add(CandidateProfileModel(account_id=a.id, active_resume_id=r.id, display_name="Test"))
        dirs = DirectionRepository(s)
        d = dirs.create(
            a.id, "Python backend", scoring_config={"semantic_selection": {"enabled": True}}
        )
        dirs.attach_resume(d.id, r.id)
        q = dirs.add_query(
            d.id,
            "Python",
            regions=regions or (SearchRegion("1", "Москва"),),
            schedule_minutes=120,
        )
        return a.id, q.id


def test_incremental_search_alternates_page_and_bounded_details_without_preparing(
    settings: Settings,
) -> None:
    account, query = seed(settings)
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, page_limit=2, detail_limit=3)
    first = cycle.run(account_id=account, search_query_id=query, browser=browser)
    assert len(browser.pages) == 1 and not browser.details
    assert first["new_vacancies"] == 4 and first["new_links"] == 4
    second = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=first)
    assert len(browser.pages) == 1 and len(browser.details) == 3
    assert second["details_loaded"] == 3
    third = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=second)
    assert browser.pages[-1][2] == 1
    assert third["coverage_exhausted"] is False
    cycle.run(account_id=account, search_query_id=query, browser=browser, progress=third)
    assert {url.rsplit("/", 1)[-1] for url in browser.details[-3:]} == {"105", "106", "107"}
    with create_database(settings).sessions() as s:
        assert s.scalar(select(func.count()).select_from(ApplicationModel)) == 0
        assert s.scalar(select(func.count()).select_from(VacancyModel)) == 8


def test_incremental_search_opens_republished_card_once(settings: Settings) -> None:
    account, query = seed(settings)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    with create_database(settings).sessions.begin() as session:
        stored_query = session.get(DirectionSearchQueryModel, query)
        assert stored_query is not None
        vacancy = VacancyRepository(session).upsert(
            VacancyData(
                "100",
                "Python",
                "https://hh.ru/vacancy/100",
                published_at=yesterday,
                description="Develop Python API",
                details_fetched_at=yesterday + timedelta(minutes=1),
            )
        )
        DirectionRepository(session).track_vacancy(stored_query.direction_id, vacancy.id)
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, page_limit=1, detail_limit=4)

    first = cycle.run(account_id=account, search_query_id=query, browser=browser)
    second = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=first)
    assert second["details_loaded"] == 3
    third = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=second)
    assert third["details_loaded"] == 1
    assert browser.details.count("https://hh.ru/vacancy/100") == 1

    fourth = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=third)
    assert fourth["pages_loaded"] == 1
    assert browser.details.count("https://hh.ru/vacancy/100") == 1


def test_new_round_reads_fresh_page_before_one_backlog_chunk(settings: Settings) -> None:
    account, query = seed(settings)
    with create_database(settings).sessions.begin() as session:
        stored_query = session.get(DirectionSearchQueryModel, query)
        assert stored_query is not None
        older = VacancyRepository(session).upsert(
            VacancyData("older-backlog", "Python", "https://hh.ru/vacancy/older-backlog")
        )
        DirectionRepository(session).track_vacancy(stored_query.direction_id, older.id)
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, page_limit=1, detail_limit=3)
    first = cycle.run(account_id=account, search_query_id=query, browser=browser)
    assert len(browser.pages) == 1 and not browser.details
    second = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=first)
    assert second["details_loaded"] == 3 and second["continuation"] is False
    third = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=second)
    assert third["details_loaded"] == 2 and third["backlog_processed"] is True
    assert third["continuation"] is True and len(browser.pages) == 1
    fourth = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=third)
    assert fourth["pages_loaded"] == 1 and len(browser.pages) == 2


def test_first_pages_of_all_regions_precede_deeper_pages(settings: Settings) -> None:
    account, query = seed(
        settings,
        regions=(SearchRegion("1", "Москва"), SearchRegion("2", "Санкт-Петербург")),
    )
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, page_limit=2, detail_limit=1)
    progress = None
    for _ in range(5):
        progress = cycle.run(
            account_id=account, search_query_id=query, browser=browser, progress=progress
        )
    assert browser.pages == [("Python", "1", 0), ("Python", "2", 0), ("Python", "1", 1)]
    assert progress is not None and progress["fresh_search_at"]


def test_fresh_page_sweep_unlocks_selection_gate_without_deep_pages(settings: Settings) -> None:
    account, query = seed(
        settings,
        regions=(SearchRegion("1", "Москва"), SearchRegion("2", "Санкт-Петербург")),
    )
    browser = Browser()
    browser.empty = True
    cycle = IncrementalSearchCycle(settings, page_limit=3)
    database = create_database(settings)
    now = datetime.now(UTC)
    progress = None
    try:
        with database.sessions.begin() as session:
            options = session.get(ApplicationSettingsModel, 1)
            assert options is not None
            options.resource_saving_mode = False
            scheduler = AutomationSchedulerService(session)
            scheduler.ensure_search_job(
                account_id=account, search_query_id=query, interval_minutes=120, now=now
            )
        for step in range(2):
            selected_at = now + timedelta(seconds=step * 16)
            with database.sessions.begin() as session:
                scheduler = AutomationSchedulerService(session)
                job = scheduler.claim_due(selected_at, allowed_kinds=(AutomationJobKind.SEARCH,))
                assert job is not None
            progress = cycle.run(
                account_id=account, search_query_id=query, browser=browser, progress=progress
            )
            with database.sessions.begin() as session:
                completed = AutomationSchedulerService(session).complete(
                    job.key, progress, selected_at
                )
                blocked = ApplicationSelectionGate(session).blocking_reason(account, selected_at)
                assert (blocked is None) is (step == 1), {
                    key: completed.last_result.get(key)
                    for key in (
                        "fresh_search_at",
                        "fresh_search_configuration",
                        "running_search_configuration",
                        "observed_page",
                        "exhausted_mask",
                    )
                }
        assert browser.pages == [("Python", "1", 0), ("Python", "2", 0)]
    finally:
        database.close()


def test_failed_page_does_not_advance_and_finished_round_returns_to_fresh_search(
    settings: Settings,
) -> None:
    account, query = seed(settings)
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, page_limit=1, detail_limit=1)
    browser.fail = True
    with pytest.raises(RuntimeError):
        cycle.run(account_id=account, search_query_id=query, browser=browser)
    browser.fail = False
    first = cycle.run(account_id=account, search_query_id=query, browser=browser)
    second = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=first)
    assert second["continuation"] is False
    third = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=second)
    assert third["details_loaded"] == 1 and third["backlog_processed"] is True
    assert third["continuation"] is True
    assert len(browser.pages) == 2
    for _ in range(3):
        third = cycle.run(
            account_id=account, search_query_id=query, browser=browser, progress=third
        )
    assert [p[2] for p in browser.pages] == [0, 0, 0]
    assert third["new_vacancies"] == 0 and third["new_links"] == 0


def test_empty_search_confirms_end_of_this_query(settings: Settings) -> None:
    account, query = seed(settings)
    browser = Browser()
    browser.empty = True
    result = IncrementalSearchCycle(settings, page_limit=3).run(
        account_id=account, search_query_id=query, browser=browser
    )
    assert result["coverage_exhausted"] is True
    assert result["round_complete"] is True
    assert result["observed_found"] == 0
    assert result["observed_page"] == 1
    assert result["pages_loaded"] == 1
    assert result["new_vacancies"] == 0
    assert browser.pages == [("Python", "1", 0)]


@pytest.mark.parametrize("changed", ["query", "region", "filters"])
def test_changed_search_conditions_reset_position(settings: Settings, changed: str) -> None:
    account, query = seed(settings)
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, page_limit=3)
    previous = cycle.run(account_id=account, search_query_id=query, browser=browser)
    assert previous["page_index"] == 1 and previous["next_step"] == "details"
    with create_database(settings).sessions.begin() as session:
        stored = session.get(DirectionSearchQueryModel, query)
        assert stored is not None
        if changed == "query":
            stored.query = "Backend"
        elif changed == "region":
            stored.regions = [{"area": "2", "name": "Санкт-Петербург"}]
        else:
            stored.filters = {"salary": 150000}
    result = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=previous
    )
    assert result["cursor_signature"] != previous["cursor_signature"]
    assert result["observed_page"] == 1 and result["page_index"] == 1
    assert "backlog_processed" not in result
    assert len(browser.pages) == 2
    assert browser.pages[-1] == (
        "Backend" if changed == "query" else "Python",
        "2" if changed == "region" else "1",
        0,
    )
    assert len(browser.details) == 0


def test_failed_later_page_retries_same_position_without_mutating_progress(
    settings: Settings,
) -> None:
    account, query = seed(settings)
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, page_limit=3, detail_limit=1)
    first = cycle.run(account_id=account, search_query_id=query, browser=browser)
    progress = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=first)
    before = deepcopy(progress)
    browser.fail = True
    with pytest.raises(RuntimeError, match="network failure"):
        cycle.run(account_id=account, search_query_id=query, browser=browser, progress=progress)
    assert progress == before
    with create_database(settings).sessions() as session:
        assert session.scalar(select(func.count()).select_from(VacancyModel)) == 4
    browser.fail = False
    retried = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=progress
    )
    assert [page[2] for page in browser.pages] == [0, 1, 1]
    assert retried["observed_page"] == 2
    assert retried["new_vacancies"] == 4
    assert retried["page_index"] == 2


def test_stopping_during_details_does_not_read_next_description(settings: Settings) -> None:
    account, query = seed(settings)
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, detail_limit=3)
    first = cycle.run(account_id=account, search_query_id=query, browser=browser)
    result = cycle.run(
        account_id=account,
        search_query_id=query,
        browser=browser,
        progress=first,
        allowed=lambda: len(browser.details) < 1,
    )
    assert len(browser.details) == 1
    assert result["details_loaded"] == 1
    assert result["details_failed"] == 0
    with create_database(settings).sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(VacancyModel)
                .where(VacancyModel.details_fetched_at.is_(None))
            )
            == 3
        )


def test_failed_descriptions_do_not_starve_older_card_across_search_turn(
    settings: Settings,
) -> None:
    account, query = seed(settings)
    browser = Browser()
    cycle = IncrementalSearchCycle(settings, page_limit=1, detail_limit=3)
    first = cycle.run(account_id=account, search_query_id=query, browser=browser)
    with create_database(settings).sessions() as session:
        vacancies = list(session.scalars(select(VacancyModel).order_by(VacancyModel.id)))
        oldest_url = vacancies[0].source_url
        browser.failed_detail_ids = {vacancy.hh_id for vacancy in vacancies[1:]}
    failed = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=first)
    assert failed["details_failed"] == 3
    assert failed["details_loaded"] == 0
    assert len(browser.details) == 3
    resumed = cycle.run(account_id=account, search_query_id=query, browser=browser, progress=failed)
    assert resumed["backlog_processed"] is True and resumed["details_failed"] == 3
    for _ in range(3):
        resumed = cycle.run(
            account_id=account, search_query_id=query, browser=browser, progress=resumed
        )
    assert oldest_url in browser.details
    assert resumed["details_loaded"] == 1
    assert len(browser.pages) == 2
    with create_database(settings).sessions() as session:
        oldest = session.scalar(select(VacancyModel).where(VacancyModel.source_url == oldest_url))
        assert oldest is not None and oldest.details_fetched_at is not None
