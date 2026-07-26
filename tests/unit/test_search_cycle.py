from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CandidateProfileModel
from hugin.domain import SearchRegion, VacancyData
from hugin.domain.hh import HhProfileData, HhResumeData
from hugin.domain.vacancies import VacancySearchResult
from hugin.repositories import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
)
from hugin.services.search_cycle import BackgroundSearchCycle

pytestmark = pytest.mark.integration


class FakeSearchBrowser:
    def __init__(self, profile: HhProfileData) -> None:
        self.profile = profile
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
        return VacancySearchResult(
            found=1,
            vacancies=(
                VacancyData(
                    hh_id="background-101",
                    title="Python backend разработчик",
                    source_url="https://hh.ru/vacancy/background-101",
                    employer_name="Пример",
                    region="Москва",
                    published_at=datetime(2026, 7, 26, 7, 0, tzinfo=UTC),
                ),
            ),
        )

    def read_vacancy_details(self, source_url: str) -> VacancyData:
        self.details.append(source_url)
        return VacancyData(
            hh_id="background-101",
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
            external_id="background-account",
            label="Тимур",
            resumes=(HhResumeData("resume-background", "Python backend разработчик"),),
        )
    )
    result = BackgroundSearchCycle(settings).run(
        account_id=account_id,
        search_query_id=query_id,
        browser=browser,
    )

    assert result["search_variants"] == 1
    assert result["unique_vacancies"] == 1
    assert result["details_loaded"] == 1
    assert result["queued"] == 1
    assert browser.searches == [("Python backend разработчик", "1", 0)]
    assert browser.details == ["https://hh.ru/vacancy/background-101"]
