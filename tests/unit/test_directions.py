from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationModel,
    ApplicationTaskModel,
    CareerDirectionModel,
    DirectionResumeModel,
    DirectionSearchQueryModel,
    DirectionVacancyModel,
)
from hugin.domain import VacancyData, VacancyState
from hugin.domain.hh import HhProfileData, HhResumeData
from hugin.repositories import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.services.hh_profile import HhProfileSyncService
from hugin.services.job_search import JobSearchSyncService
from hugin.services.vacancy_analysis import VacancyAnalysisService

pytestmark = pytest.mark.integration


def test_hh_profile_sync_updates_account_and_resumes(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            service = HhProfileSyncService(session)
            created = service.synchronize(
                HhProfileData(
                    external_id="account-123",
                    label="Иван Иванов",
                    resumes=(
                        HhResumeData("resume-1", "Python-разработчик"),
                        HhResumeData("resume-2", "Инженер"),
                    ),
                )
            )
            updated = service.synchronize(
                HhProfileData(
                    external_id="account-123",
                    label="Иван Петров",
                    resumes=(
                        HhResumeData("resume-2", "Ведущий инженер"),
                        HhResumeData("resume-3", "Руководитель"),
                    ),
                )
            )

            assert updated.account.id == created.account.id
            assert updated.account.label == "Иван Петров"
            assert [resume.hh_id for resume in updated.resumes] == ["resume-2", "resume-3"]
            stored = ResumeRepository(session).list_by_account_id(updated.account.id)
            assert [(resume.hh_id, resume.title, resume.is_active) for resume in stored] == [
                ("resume-1", "Python-разработчик", False),
                ("resume-2", "Ведущий инженер", True),
                ("resume-3", "Руководитель", True),
            ]
    finally:
        database.close()


def test_job_search_sync_is_repeatable_and_does_not_create_applications(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            profile = HhProfileSyncService(session).synchronize(
                HhProfileData(
                    external_id="account-123",
                    label="Иван Иванов",
                    resumes=(HhResumeData("resume-1", "Python-разработчик"),),
                )
            )
            assert profile.account.id > 0
            service = JobSearchSyncService(session)
            created = service.synchronize(
                account_external_id="account-123",
                direction_name="Python backend",
                resume_title="Python-разработчик",
                query="Python backend",
                area="113",
                filters={"order_by": "publication_time"},
                vacancies=(VacancyData("100", "Python developer", "https://hh.ru/vacancy/100"),),
            )
            updated = service.synchronize(
                account_external_id="account-123",
                direction_name="Python backend",
                resume_title="Python-разработчик",
                query="Python backend",
                area="113",
                filters={"order_by": "publication_time"},
                vacancies=(
                    VacancyData("100", "Python backend", "https://hh.ru/vacancy/100"),
                    VacancyData("200", "Backend developer", "https://hh.ru/vacancy/200"),
                ),
            )

            assert updated.direction.id == created.direction.id
            assert updated.query.id == created.query.id
            assert [vacancy.hh_id for vacancy in updated.vacancies] == ["100", "200"]
            assert session.scalar(select(func.count()).select_from(CareerDirectionModel)) == 1
            assert session.scalar(select(func.count()).select_from(DirectionSearchQueryModel)) == 1
            assert session.scalar(select(func.count()).select_from(DirectionResumeModel)) == 1
            assert session.scalar(select(func.count()).select_from(DirectionVacancyModel)) == 2
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 0
            assert session.scalar(select(func.count()).select_from(ApplicationTaskModel)) == 0

            analyzed = VacancyAnalysisService(session).synchronize(
                account_external_id="account-123",
                direction_name="Python backend",
                vacancies=(
                    VacancyData(
                        "100",
                        "Python backend разработчик",
                        "https://hh.ru/vacancy/100",
                        description="FastAPI, PostgreSQL, Docker",
                        experience="1-3 года",
                        key_skills=("Python", "FastAPI"),
                        details_fetched_at=datetime.now(UTC),
                    ),
                    VacancyData(
                        "200",
                        "Продуктовый аналитик",
                        "https://hh.ru/vacancy/200",
                        description="Python и SQL",
                        details_fetched_at=datetime.now(UTC),
                    ),
                ),
            )

            assert [result.evaluation.accepted for result in analyzed] == [True, False]
            links = list(
                session.scalars(
                    select(DirectionVacancyModel).order_by(DirectionVacancyModel.vacancy_id)
                )
            )
            assert [link.state for link in links] == [
                VacancyState.FILTERED,
                VacancyState.SKIPPED,
            ]
            assert links[0].rules_details["accepted"] is True
            assert links[1].rules_details["accepted"] is False
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 0
            assert session.scalar(select(func.count()).select_from(ApplicationTaskModel)) == 0

            repeated = VacancyAnalysisService(session).reanalyze(
                account_external_id="account-123",
                direction_name="Python backend",
            )
            assert [result.vacancy.hh_id for result in repeated] == ["100", "200"]
    finally:
        database.close()


def test_directions_support_multiple_resumes_and_queries(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Main account", "account-1")
            directions = DirectionRepository(session)
            backend = directions.create(
                account.id,
                "Backend",
                scoring_config={"minimum_score": 70},
            )
            automation = directions.create(account.id, "Automation")
            resumes = ResumeRepository(session)
            developer_resume = resumes.upsert(account.id, "resume-1", "Python developer")
            automation_resume = resumes.upsert(account.id, "resume-2", "Automation engineer")

            directions.attach_resume(backend.id, developer_resume.id)
            directions.attach_resume(automation.id, developer_resume.id)
            directions.attach_resume(automation.id, automation_resume.id, priority=10)
            query = directions.add_query(
                backend.id,
                "Python backend",
                area="1",
                filters={"experience": ["between3And6"]},
            )

            assert backend.scoring_config == {"minimum_score": 70}
            assert query.filters == {"experience": ["between3And6"]}
            assert directions.list_resumes(backend.id) == [developer_resume]
            assert directions.list_resumes(automation.id) == [
                automation_resume,
                developer_resume,
            ]
            assert session.scalar(select(func.count()).select_from(DirectionResumeModel)) == 3
    finally:
        database.close()


def test_direction_rejects_resume_from_another_account(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            accounts = AccountRepository(session)
            first = accounts.create("First")
            second = accounts.create("Second")
            direction = DirectionRepository(session).create(first.id, "Backend")
            resume = ResumeRepository(session).upsert(second.id, "resume-2", "Other resume")

            with pytest.raises(ValueError, match="same account"):
                DirectionRepository(session).attach_resume(direction.id, resume.id)
    finally:
        database.close()


def test_vacancy_is_tracked_independently_for_each_direction(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Main account")
            directions = DirectionRepository(session)
            backend = directions.create(account.id, "Backend")
            automation = directions.create(account.id, "Automation")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="1000",
                    title="Python developer",
                    source_url="https://hh.ru/vacancy/1000",
                )
            )

            first = directions.track_vacancy(backend.id, vacancy.id)
            second = directions.track_vacancy(automation.id, vacancy.id)

            assert first.state is VacancyState.DISCOVERED
            assert second.state is VacancyState.DISCOVERED
            assert first.direction_id != second.direction_id
            assert session.scalar(select(func.count()).select_from(DirectionVacancyModel)) == 2
    finally:
        database.close()
