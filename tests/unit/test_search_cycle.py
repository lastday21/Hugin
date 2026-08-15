from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CandidateProfileModel
from hugin.domain import SearchRegion, VacancyData, VacancyState
from hugin.domain.hh import HhProfileData, HhResumeData
from hugin.domain.vacancies import VacancySearchResult
from hugin.repositories import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.services.search_cycle import BackgroundSearchCycle
from hugin.services.vacancy_analysis import RULES_VERSION

pytestmark = pytest.mark.integration


class FakeSearchBrowser:
    def __init__(
        self,
        profile: HhProfileData,
        *,
        fail_page: int | None = None,
    ) -> None:
        self.profile = profile
        self.fail_page = fail_page
        self.searches: list[tuple[str, str, int]] = []
        self.details: list[str] = []

    def read_profile(self) -> HhProfileData:
        return self.profile

    def search_vacancies(
        self,
        query: str,
        *,
        area: str = "",
        filters: dict[str, object] | None = None,
        page_number: int = 0,
    ) -> VacancySearchResult:
        assert filters is not None
        self.searches.append((query, area, page_number))
        if page_number == self.fail_page:
            raise RuntimeError("страница поиска временно недоступна")
        return VacancySearchResult(
            found=2,
            vacancies=(
                VacancyData(
                    hh_id=f"background-10{page_number + 1}",
                    title="Python backend разработчик",
                    source_url=f"https://hh.ru/vacancy/background-10{page_number + 1}",
                    employer_name="Пример",
                    region="Москва",
                    published_at=datetime(2026, 7, 26, 7, 0, tzinfo=UTC),
                ),
            ),
        )

    def read_vacancy_details(self, source_url: str) -> VacancyData:
        self.details.append(source_url)
        hh_id = source_url.rsplit("/", 1)[-1]
        return VacancyData(
            hh_id=hh_id,
            title="Python backend разработчик",
            source_url=source_url,
            employer_name="Пример",
            description="Разработка серверной части на Python и PostgreSQL",
            experience="3-6 лет",
            employment="Полная занятость",
            work_format="Удалённо",
            key_skills=("Python", "PostgreSQL"),
            region="Москва",
            published_at=datetime(2026, 7, 26, 7, 0, tzinfo=UTC),
            details_fetched_at=datetime(2026, 7, 26, 8, 0, tzinfo=UTC),
        )


def test_background_search_reads_and_queues_without_applying(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тимур", "background-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-background",
                "Python backend разработчик",
            )
            session.add(
                CandidateProfileModel(
                    account_id=account.id,
                    active_resume_id=resume.id,
                    display_name="Тимур",
                )
            )
            directions = DirectionRepository(session)
            direction = directions.create(
                account.id,
                "Python backend",
                scoring_config={"role_scope": "PYTHON_BACKEND"},
            )
            directions.attach_resume(direction.id, resume.id)
            legacy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="legacy-v4",
                    title="Python backend разработчик",
                    source_url="https://hh.ru/vacancy/legacy-v4",
                    description="Разработка серверной части на Python и PostgreSQL",
                    key_skills=("Python", "PostgreSQL"),
                    published_at=datetime(2026, 7, 25, 7, 0, tzinfo=UTC),
                    details_fetched_at=datetime(2026, 7, 25, 8, 0, tzinfo=UTC),
                )
            )
            directions.track_vacancy(direction.id, legacy.id)
            directions.apply_rules(
                direction.id,
                legacy.id,
                state=VacancyState.FILTERED_OUT,
                score=0,
                details={"category": "REJECTED", "accepted": False},
                rules_version="python_it_v4",
            )
            query = directions.add_query(
                direction.id,
                "Python backend разработчик",
                regions=(SearchRegion("1", "Москва"),),
                schedule_minutes=120,
            )
            account_id = account.id
            direction_id = direction.id
            query_id = query.id
    finally:
        database.close()

    browser = FakeSearchBrowser(
        HhProfileData(
            external_id="background-account",
            label="Тимур",
            resumes=(HhResumeData("resume-background", "Python backend разработчик"),),
        )
    )
    result = BackgroundSearchCycle(settings, page_limit=2, detail_limit=1).run(
        account_id=account_id,
        search_query_id=query_id,
        browser=browser,
    )

    assert result["search_variants"] == 1
    assert result["pages_loaded"] == 2
    assert result["found"] == 2
    assert result["unique_vacancies"] == 2
    assert result["details_loaded"] == 1
    assert result["queued"] == 2
    assert browser.searches == [
        ("Python backend разработчик", "1", 0),
        ("Python backend разработчик", "1", 1),
    ]
    assert browser.details == ["https://hh.ru/vacancy/background-102"]

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            first = VacancyRepository(session).get_by_hh_id("background-101")
            second = VacancyRepository(session).get_by_hh_id("background-102")
            assert first is not None
            assert second is not None
            assert first.details_fetched_at is None
            assert second.details_fetched_at is not None
            legacy_vacancy = VacancyRepository(session).get_by_hh_id("legacy-v4")
            assert legacy_vacancy is not None
            legacy_link = DirectionRepository(session).get_tracked_vacancy(
                direction_id,
                legacy_vacancy.id,
            )
            assert legacy_link.rules_version == RULES_VERSION
            assert legacy_link.state is VacancyState.QUEUED
            assert legacy_link.rules_details["category"] == "MATCH"
    finally:
        database.close()


def test_successful_page_is_saved_before_later_page_failure(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тимур", "partial-search-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-partial-search",
                "Python backend разработчик",
            )
            session.add(
                CandidateProfileModel(
                    account_id=account.id,
                    active_resume_id=resume.id,
                    display_name="Тимур",
                )
            )
            directions = DirectionRepository(session)
            direction = directions.create(
                account.id,
                "Python backend",
                scoring_config={"role_scope": "PYTHON_BACKEND"},
            )
            directions.attach_resume(direction.id, resume.id)
            query = directions.add_query(
                direction.id,
                "Python backend разработчик",
                regions=(SearchRegion("1", "Москва"),),
                schedule_minutes=120,
            )
            account_id = account.id
            query_id = query.id
    finally:
        database.close()

    browser = FakeSearchBrowser(
        HhProfileData(
            external_id="partial-search-account",
            label="Тимур",
            resumes=(HhResumeData("resume-partial-search", "Python backend разработчик"),),
        ),
        fail_page=1,
    )

    with pytest.raises(RuntimeError, match="страница поиска"):
        BackgroundSearchCycle(settings, page_limit=3, detail_limit=1).run(
            account_id=account_id,
            search_query_id=query_id,
            browser=browser,
        )

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            saved = VacancyRepository(session).get_by_hh_id("background-101")
            assert saved is not None
            assert saved.source_url == "https://hh.ru/vacancy/background-101"
    finally:
        database.close()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"page_limit": 0}, "страниц"),
        ({"detail_limit": 0}, "подробной загрузки"),
    ],
)
def test_background_search_rejects_non_positive_limits(
    settings: Settings,
    values: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        BackgroundSearchCycle(settings, **values)


def test_background_search_rejects_another_hh_account(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle = BackgroundSearchCycle(settings)
    monkeypatch.setattr(
        cycle,
        "_tasks",
        lambda _account_id, _search_query_id: ("expected-account", "Python backend", ()),
    )
    browser = FakeSearchBrowser(
        HhProfileData(
            external_id="another-account",
            label="Другой пользователь",
            resumes=(),
        )
    )

    with pytest.raises(RuntimeError, match="выбран неверно"):
        cycle.run(account_id=1, search_query_id=1, browser=browser)
