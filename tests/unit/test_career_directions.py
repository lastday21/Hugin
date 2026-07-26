from __future__ import annotations

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    CandidateProfileModel,
    DirectionSearchQueryModel,
    VacancyDiscoveryModel,
    VerifiedFactModel,
)
from hugin.domain.content import ConfirmationState
from hugin.domain.directions import DirectionScope, EmploymentForm, SearchRegion, WorkFormat
from hugin.domain.vacancies import VacancyData
from hugin.repositories.directions import AccountRepository, ResumeRepository
from hugin.services.career_directions import (
    DEFAULT_DIRECTION_QUERIES,
    CareerDirectionService,
)
from hugin.services.job_search import JobSearchSyncService

pytestmark = pytest.mark.integration


def test_direction_settings_use_active_resume_and_build_city_searches(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тимур", "account-1")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-1",
                "Python-разработчик",
            )
            profile = CandidateProfileModel(
                account_id=account.id,
                active_resume_id=resume.id,
                display_name="Тимур",
            )
            session.add(profile)
            session.flush()
            for category, content in (
                ("desired_position", "Python backend разработчик"),
                ("work_format", "Удалённо или в офисе"),
                ("employment", "Полная занятость"),
                ("salary_expectation", "Минимум 180 000, желательно 220 000 рублей"),
                ("skills", "Python, FastAPI, PostgreSQL"),
            ):
                session.add(
                    VerifiedFactModel(
                        profile_id=profile.id,
                        resume_id=resume.id,
                        category=category,
                        content=content,
                        source_type="resume",
                        state=ConfirmationState.CONFIRMED,
                    )
                )
            session.flush()

            configured = CareerDirectionService(session).configure(
                account_id=account.id,
                direction_name="ИТ",
                queries=None,
                regions=(SearchRegion("1", "Москва"), SearchRegion("3", "Екатеринбург")),
            )
            tasks = CareerDirectionService(session).build_search_tasks(account.id, "ИТ")

            assert configured.resume == resume
            assert (
                tuple(query.query for query in configured.queries)
                == (DEFAULT_DIRECTION_QUERIES[DirectionScope.IT_ADJACENT])
            )
            assert configured.role_scope is DirectionScope.IT_ADJACENT
            assert configured.work_formats == (WorkFormat.REMOTE, WorkFormat.ON_SITE)
            assert configured.employment_forms == (EmploymentForm.FULL,)
            assert configured.minimum_salary == 180_000
            assert configured.desired_salary == 220_000
            assert configured.skills_from_resume == ("Python, FastAPI, PostgreSQL",)
            assert [(task.area, task.region_name) for task in tasks] == [
                ("1", "Москва"),
                ("3", "Екатеринбург"),
                ("113", "Россия — удалённо"),
            ] * len(DEFAULT_DIRECTION_QUERIES[DirectionScope.IT_ADJACENT])
            assert tasks[0].filters == {
                "order_by": "publication_time",
                "employment_form": ["FULL"],
                "salary": 180_000,
                "currency": "RUR",
                "only_with_salary": True,
                "work_format": ["ON_SITE"],
            }
            assert tasks[2].filters["work_format"] == ["REMOTE"]
            synchronized = JobSearchSyncService(session).synchronize(
                account_external_id="account-1",
                direction_name="ИТ",
                resume_title=None,
                query=tasks[0].query,
                area=tasks[0].area,
                region=tasks[0].region_name,
                search_query_id=tasks[0].search_query_id,
                filters=tasks[0].filters,
                vacancies=(
                    VacancyData(
                        "vacancy-1",
                        "Python-разработчик",
                        "https://hh.ru/vacancy/vacancy-1",
                    ),
                ),
            )
            assert synchronized.resume == resume
            assert session.scalar(select(func.count()).select_from(DirectionSearchQueryModel)) == 6
            assert session.scalar(select(func.count()).select_from(VacancyDiscoveryModel)) == 1

            updated = CareerDirectionService(session).update(
                account_id=account.id,
                direction_id=configured.direction.id,
                is_active=False,
                queries=("Fullstack Python", "Интеграции Python API"),
                regions=(SearchRegion("88", "Казань"),),
                work_formats=(WorkFormat.REMOTE, WorkFormat.HYBRID),
                employment_forms=(EmploymentForm.FULL, EmploymentForm.PROJECT),
                minimum_salary=160_000,
                desired_salary=230_000,
                remote_all_russia=True,
                schedule_minutes=90,
            )
            assert updated.direction.is_active is False
            assert tuple(query.query for query in updated.queries) == (
                "Fullstack Python",
                "Интеграции Python API",
            )
            assert updated.queries[0].regions == (SearchRegion("88", "Казань"),)
            assert updated.work_formats == (WorkFormat.REMOTE, WorkFormat.HYBRID)
            assert updated.employment_forms == (EmploymentForm.FULL, EmploymentForm.PROJECT)
            assert updated.minimum_salary == 160_000
            assert updated.desired_salary == 230_000
            assert updated.remote_all_russia is True
            assert updated.queries[0].schedule_minutes == 90
    finally:
        database.close()


def test_direction_without_cities_searches_all_russia(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тимур", "account-2")
            resume = ResumeRepository(session).upsert(account.id, "resume-2", "Разработчик")
            session.add(
                CandidateProfileModel(
                    account_id=account.id,
                    active_resume_id=resume.id,
                    display_name="Тимур",
                )
            )
            session.flush()

            configured = CareerDirectionService(session).configure(
                account_id=account.id,
                direction_name="ИТ",
                queries=None,
                regions=(),
            )
            tasks = CareerDirectionService(session).build_search_tasks(account.id, "ИТ")

            assert (
                tuple(query.query for query in configured.queries)
                == (DEFAULT_DIRECTION_QUERIES[DirectionScope.IT_ADJACENT])
            )
            assert configured.queries[0].regions == (SearchRegion("113", "Россия"),)
            assert configured.work_formats == ()
            assert configured.employment_forms == ()
            assert configured.minimum_salary is None
            assert [(task.area, task.region_name) for task in tasks] == [
                ("113", "Россия"),
            ] * len(DEFAULT_DIRECTION_QUERIES[DirectionScope.IT_ADJACENT])
            assert tasks[0].filters == {"order_by": "publication_time"}
    finally:
        database.close()
