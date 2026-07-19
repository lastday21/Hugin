from __future__ import annotations

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import DirectionResumeModel, DirectionVacancyModel
from hugin.domain import VacancyData, VacancyState
from hugin.repositories import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
    VacancyRepository,
)

pytestmark = pytest.mark.integration


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
