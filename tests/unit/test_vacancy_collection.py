from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain import VacancyData, VacancyState
from hugin.repositories import AccountRepository, DirectionRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.vacancy_analysis import RuleCategory, VacancyAnalysisService
from hugin.services.vacancy_review import VacancyReviewService

pytestmark = pytest.mark.integration


def detailed_vacancy(
    hh_id: str,
    title: str,
    *,
    employer: str = "Ромашка",
    description: str = "Обязанности\nРазрабатывать API на Python и FastAPI для внутренних служб.",
) -> VacancyData:
    return VacancyData(
        hh_id=hh_id,
        title=title,
        source_url=f"https://hh.ru/vacancy/{hh_id}",
        employer_name=employer,
        description=description,
        experience="1-3 года",
        work_format="Удалённо",
        key_skills=("Python", "FastAPI", "PostgreSQL"),
        details_fetched_at=datetime.now(UTC),
        region="Москва",
        salary_from=Decimal("120000"),
        salary_to=Decimal("180000"),
        salary_currency="RUR",
        responsibilities="Разрабатывать API на Python и FastAPI для внутренних служб.",
        required_qualifications="Python, FastAPI, PostgreSQL",
    )


def test_collection_tracks_changes_discoveries_duplicates_and_rejected(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тест", "account-vacancies")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "ИТ")
            directions.attach_resume(direction.id, resume.id)
            query = directions.add_query(direction.id, "Python backend", area="1")

            service = VacancyAnalysisService(session)
            results = service.synchronize(
                account_external_id="account-vacancies",
                direction_name="ИТ",
                vacancies=(
                    detailed_vacancy("100", "Python backend разработчик"),
                    detailed_vacancy("101", "Python backend-разработчик"),
                    detailed_vacancy(
                        "102",
                        "Продуктовый аналитик",
                        employer="Другая компания",
                        description="Требования\nPython и SQL для продуктовой аналитики.",
                    ),
                ),
            )

            assert [result.evaluation.category for result in results] == [
                RuleCategory.MATCH,
                RuleCategory.MATCH,
                RuleCategory.REJECTED,
            ]
            assert results[1].state is VacancyState.ANALYZED
            assert results[1].vacancy.duplicate_of_id == results[0].vacancy.id
            assert any(
                "обрабатывается отдельно" in reason
                for reason in results[1].evaluation.reasons
            )

            directions.record_discovery(
                direction_id=direction.id,
                search_query_id=query.id,
                vacancy_id=results[0].vacancy.id,
                query_text="Python backend",
                region="Москва",
            )
            directions.record_discovery(
                direction_id=direction.id,
                search_query_id=query.id,
                vacancy_id=results[0].vacancy.id,
                query_text="Python backend",
                region="Москва",
            )

            repository = VacancyRepository(session)
            updated = repository.upsert(
                detailed_vacancy("100", "Python backend разработчик (FastAPI)")
            )
            assert updated.id == results[0].vacancy.id
            repository.upsert(detailed_vacancy("100", "Python backend разработчик (FastAPI)"))
            assert [event.event_type for event in repository.list_changes(updated.id)] == [
                "CREATED",
                "UPDATED",
            ]
            assert len(repository.list_discoveries(updated.id)) == 1

            review = VacancyReviewService(session)
            rejected = review.list_rejected(
                account_id=account.id,
                direction_name="ИТ",
                company="другая",
                reason="аналитика",
            )
            assert [entry.vacancy.hh_id for entry in rejected] == ["102"]
            restored = review.restore(
                account_id=account.id,
                direction_name="ИТ",
                hh_id="102",
            )
            assert restored.tracking.state is VacancyState.ANALYZED
            assert restored.tracking.rules_details["manual_override"] == "ACCEPT"
    finally:
        database.close()


def test_rejected_list_validates_sort_and_restore_state(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тест", "account-review")
            direction = DirectionRepository(session).create(account.id, "ИТ")
            vacancy = VacancyRepository(session).upsert(
                detailed_vacancy("200", "Python разработчик")
            )
            DirectionRepository(session).track_vacancy(direction.id, vacancy.id)
            review = VacancyReviewService(session)

            with pytest.raises(ValueError, match="Сортировка"):
                review.list_rejected(
                    account_id=account.id,
                    direction_name="ИТ",
                    sort="unknown",
                )
            with pytest.raises(ValueError, match="не находится"):
                review.restore(account_id=account.id, direction_name="ИТ", hh_id="200")
    finally:
        database.close()
