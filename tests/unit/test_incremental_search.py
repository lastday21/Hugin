from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationModel,
    CandidateProfileModel,
    DirectionSearchQueryModel,
    VacancyModel,
)
from hugin.domain import SearchRegion, VacancyData
from hugin.domain.hh import HhProfileData, HhResumeData
from hugin.domain.vacancies import VacancySearchResult
from hugin.repositories import AccountRepository, DirectionRepository, ResumeRepository
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


def seed(settings: Settings) -> tuple[int, int]:
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
            d.id, "Python", regions=(SearchRegion("1", "Москва"),), schedule_minutes=120
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
    with create_database(settings).sessions() as s:
        assert s.scalar(select(func.count()).select_from(ApplicationModel)) == 0
        assert s.scalar(select(func.count()).select_from(VacancyModel)) == 8


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
    assert result["observed_page"] == 1
    assert len(browser.pages) == 2
    assert browser.pages[-1] == (
        "Backend" if changed == "query" else "Python",
        "2" if changed == "region" else "1",
        0,
    )
    assert browser.details == []


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
    searched = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=failed
    )
    assert searched["new_vacancies"] == 0
    resumed = cycle.run(
        account_id=account, search_query_id=query, browser=browser, progress=searched
    )
    assert browser.details[3] == oldest_url
    assert resumed["details_loaded"] == 1
    with create_database(settings).sessions() as session:
        oldest = session.scalar(select(VacancyModel).where(VacancyModel.source_url == oldest_url))
        assert oldest is not None and oldest.details_fetched_at is not None
