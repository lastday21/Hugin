from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import ApplicationModel, DirectionVacancyModel
from hugin.domain import VacancyAvailability, VacancyData, VacancyState
from hugin.domain.applications import ApplicationState
from hugin.domain.directions import DirectionScope
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    ResumeRepository,
)
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.vacancy_analysis import (
    RULES_VERSION,
    RuleCategory,
    VacancyAnalysisService,
)
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
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            query = directions.add_query(direction.id, "Python backend", area="1")

            service = VacancyAnalysisService(session)
            results = service.synchronize(
                account_external_id="account-vacancies",
                direction_name="Python backend",
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
                RuleCategory.REJECTED,
                RuleCategory.REJECTED,
            ]
            assert results[1].state is VacancyState.FILTERED_OUT
            assert results[1].vacancy.duplicate_of_id == results[0].vacancy.id
            assert "дубликат вакансии" in results[1].evaluation.reasons

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
            review.restore(
                account_id=account.id,
                direction_name="Python backend",
                hh_id="101",
            )
            rechecked = service.reanalyze(
                account_external_id="account-vacancies",
                direction_name="Python backend",
            )
            duplicate = next(item for item in rechecked if item.vacancy.hh_id == "101")
            assert duplicate.evaluation.category is RuleCategory.REJECTED
            assert duplicate.state is VacancyState.FILTERED_OUT

            rejected = review.list_rejected(
                account_id=account.id,
                direction_name="Python backend",
                company="другая",
                reason="аналитика",
            )
            assert [entry.vacancy.hh_id for entry in rejected] == ["102"]
            restored = review.restore(
                account_id=account.id,
                direction_name="Python backend",
                hh_id="102",
            )
            assert restored.tracking.state is VacancyState.ANALYZED
            assert restored.tracking.rules_details["manual_override"] == "ACCEPT"

            tracking = session.get(
                DirectionVacancyModel,
                (direction.id, restored.vacancy.id),
            )
            assert tracking is not None
            tracking.rules_version = "python_it_previous"
            reanalyzed = service.reanalyze(
                account_external_id="account-vacancies",
                direction_name="Python backend",
            )
            refreshed = next(item for item in reanalyzed if item.vacancy.hh_id == "102")
            assert refreshed.evaluation.category is RuleCategory.REJECTED
            assert refreshed.state is VacancyState.FILTERED_OUT
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


def test_review_can_accept_queued_stretch_vacancy(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тест", "account-stretch-review")
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            vacancy = VacancyRepository(session).upsert(
                detailed_vacancy("stretch-200", "Python backend разработчик")
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=85,
                details={
                    "accepted": True,
                    "category": "STRETCH",
                    "reasons": ["требуется ручная проверка"],
                },
            )

            restored = VacancyReviewService(session).restore(
                account_id=account.id,
                direction_name="Python backend",
                hh_id="stretch-200",
            )

            assert restored.tracking.state is VacancyState.ANALYZED
            assert restored.tracking.rules_details["category"] == "MATCH"
            assert restored.tracking.rules_details["manual_override"] == "ACCEPT"
    finally:
        database.close()


def test_exact_body_repost_with_changed_title_is_not_queued_twice(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тест", "duplicate-title-account")
            assert account.external_id is not None
            resume = ResumeRepository(session).upsert(account.id, "resume-duplicate", "Python")
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            service = VacancyAnalysisService(session)

            results = service.synchronize(
                account_external_id=account.external_id,
                direction_name=direction.name,
                vacancies=(
                    detailed_vacancy("duplicate-title-1", "Junior Python (стажер)"),
                    detailed_vacancy(
                        "duplicate-title-2",
                        "Junior Python-разработчик (стажер)",
                    ),
                ),
            )

            assert results[1].vacancy.duplicate_of_id == results[0].vacancy.id
            prepared = ApplicationAutomationService(session).prepare(
                account_external_id=account.external_id,
                direction_name=direction.name,
                include_stretch=False,
            )
            assert prepared.created == 1
            assert session.scalar(select(func.count(ApplicationModel.id))) == 1
    finally:
        database.close()


def test_duplicate_detection_does_not_depend_on_detail_fetch_order(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create(
                "Тест",
                "duplicate-detail-order-account",
            )
            assert account.external_id is not None
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-detail-order",
                "Python",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)

            repository = VacancyRepository(session)
            title = "Python backend разработчик"
            earlier = repository.upsert(
                VacancyData(
                    "136547864",
                    title,
                    "https://hh.ru/vacancy/136547864",
                    employer_name="Ромашка",
                )
            )
            later = repository.upsert(
                VacancyData(
                    "136547866",
                    title,
                    "https://hh.ru/vacancy/136547866",
                    employer_name="Ромашка",
                )
            )
            assert earlier.id < later.id

            analysis = VacancyAnalysisService(session)
            later_result = analysis.synchronize(
                account_external_id=account.external_id,
                direction_name=direction.name,
                vacancies=(detailed_vacancy("136547866", title),),
            )
            assert later_result[0].vacancy.duplicate_of_id is None

            automation = ApplicationAutomationService(session)
            assert (
                automation.prepare_vacancies(
                    account_external_id=account.external_id,
                    vacancy_ids=(later.id,),
                    include_stretch=False,
                )
                == 1
            )

            earlier_result = analysis.synchronize(
                account_external_id=account.external_id,
                direction_name=direction.name,
                vacancies=(detailed_vacancy("136547864", title),),
            )

            assert earlier_result[0].vacancy.duplicate_of_id == later.id
            assert repository.get(later.id).duplicate_of_id is None
            assert (
                automation.prepare_vacancies(
                    account_external_id=account.external_id,
                    vacancy_ids=(earlier.id,),
                    include_stretch=False,
                )
                == 0
            )
            assert session.scalar(select(func.count(ApplicationModel.id))) == 1
    finally:
        database.close()


def test_reanalysis_promotes_active_duplicate_with_archived_canonical(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create(
                "Тест",
                "persisted-duplicate-account",
            )
            assert account.external_id is not None
            resume = ResumeRepository(session).upsert(
                account.id,
                "persisted-duplicate-resume",
                "Python",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)

            vacancies = VacancyRepository(session)
            canonical = vacancies.upsert(
                detailed_vacancy("persisted-canonical", "Python backend разработчик")
            )
            duplicate = vacancies.upsert(
                detailed_vacancy("persisted-duplicate", "Python backend разработчик")
            )
            vacancies.mark_duplicate(duplicate.id, canonical.id, 0.99)
            directions.track_vacancy(direction.id, canonical.id)
            vacancies.mark_unavailable(canonical.id, VacancyAvailability.ARCHIVED)
            directions.track_vacancy(direction.id, duplicate.id)
            directions.apply_rules(
                direction.id,
                duplicate.id,
                state=VacancyState.ANALYZED,
                score=90,
                details={
                    "accepted": True,
                    "category": RuleCategory.MATCH.value,
                    "manual_override": "ACCEPT",
                    "reasons": ["решение изменено пользователем"],
                },
                rules_version=RULES_VERSION,
            )

            stored = vacancies.get(duplicate.id)
            assert stored.duplicate_of_id == canonical.id
            assert vacancies.list_duplicate_candidates(stored) == []

            reanalyzed = VacancyAnalysisService(session).reanalyze(
                account_external_id=account.external_id,
                direction_name=direction.name,
            )
            result = next(item for item in reanalyzed if item.vacancy.id == duplicate.id)
            canonical_result = next(item for item in reanalyzed if item.vacancy.id == canonical.id)

            assert result.evaluation.category is RuleCategory.MATCH
            assert result.evaluation.accepted
            assert result.state is VacancyState.ANALYZED
            assert result.vacancy.duplicate_of_id is None
            assert vacancies.get(duplicate.id).duplicate_of_id is None
            assert vacancies.get(canonical.id).duplicate_of_id == duplicate.id
            assert canonical_result.evaluation.category is RuleCategory.REJECTED
            assert canonical_result.state is VacancyState.CLOSED
            assert (
                directions.get_tracked_vacancy(direction.id, canonical.id).rules_details["category"]
                == RuleCategory.REJECTED.value
            )

            prepared = ApplicationAutomationService(session).prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=False,
            )

            assert prepared.created == 1
            application = session.scalar(select(ApplicationModel))
            assert application is not None
            assert application.vacancy_id == duplicate.id
    finally:
        database.close()


def test_reanalysis_uses_updated_duplicate_family_after_promotion(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create(
                "Тест",
                "duplicate-family-promotion-account",
            )
            assert account.external_id is not None
            resume = ResumeRepository(session).upsert(
                account.id,
                "duplicate-family-promotion-resume",
                "Python",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)

            vacancies = VacancyRepository(session)
            first_active = vacancies.upsert(
                detailed_vacancy("family-active-first", "Python backend разработчик")
            )
            old_canonical = vacancies.upsert(
                detailed_vacancy("family-old-canonical", "Python backend разработчик")
            )
            later_active = vacancies.upsert(
                detailed_vacancy("family-active-later", "Python backend разработчик")
            )
            vacancies.mark_duplicate(first_active.id, old_canonical.id, 0.99)
            vacancies.mark_duplicate(later_active.id, old_canonical.id, 0.99)
            for vacancy in (first_active, old_canonical, later_active):
                directions.track_vacancy(direction.id, vacancy.id)
            vacancies.mark_unavailable(
                old_canonical.id,
                VacancyAvailability.ARCHIVED,
            )

            results = VacancyAnalysisService(session).reanalyze(
                account_external_id=account.external_id,
                direction_name=direction.name,
            )
            by_id = {item.vacancy.id: item for item in results}

            assert by_id[first_active.id].evaluation.category is RuleCategory.MATCH
            assert by_id[first_active.id].vacancy.duplicate_of_id is None
            assert by_id[old_canonical.id].evaluation.category is RuleCategory.REJECTED
            assert by_id[old_canonical.id].state is VacancyState.CLOSED
            assert by_id[later_active.id].evaluation.category is RuleCategory.REJECTED
            assert by_id[later_active.id].state is VacancyState.FILTERED_OUT
            assert vacancies.get(old_canonical.id).duplicate_of_id == first_active.id
            assert vacancies.get(later_active.id).duplicate_of_id == first_active.id

            prepared = ApplicationAutomationService(session).prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=False,
            )

            assert prepared.created == 1
            application = session.scalar(select(ApplicationModel))
            assert application is not None
            assert application.vacancy_id == first_active.id
    finally:
        database.close()


def test_reanalysis_keeps_duplicate_blocked_when_family_has_sent_application(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create(
                "Тест",
                "persisted-duplicate-sent-account",
            )
            assert account.external_id is not None
            resume = ResumeRepository(session).upsert(
                account.id,
                "persisted-duplicate-sent-resume",
                "Python",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)

            vacancies = VacancyRepository(session)
            canonical = vacancies.upsert(
                detailed_vacancy("persisted-sent-canonical", "Python backend разработчик")
            )
            duplicate = vacancies.upsert(
                detailed_vacancy("persisted-sent-duplicate", "Python backend разработчик")
            )
            vacancies.mark_duplicate(duplicate.id, canonical.id, 0.99)
            directions.track_vacancy(direction.id, canonical.id)
            directions.track_vacancy(direction.id, duplicate.id)
            directions.apply_rules(
                direction.id,
                duplicate.id,
                state=VacancyState.ANALYZED,
                score=90,
                details={
                    "accepted": True,
                    "category": RuleCategory.MATCH.value,
                    "manual_override": "ACCEPT",
                    "reasons": ["решение изменено пользователем"],
                },
                rules_version=RULES_VERSION,
            )
            applications = ApplicationRepository(session)
            application = applications.create_apply_intent(
                account.id,
                canonical.id,
                resume.id,
                direction.id,
            )
            applications.transition_state(
                application.id,
                ApplicationState.APPLIED,
                {"source": "hh.ru"},
            )
            vacancies.mark_unavailable(canonical.id, VacancyAvailability.ARCHIVED)

            reanalyzed = VacancyAnalysisService(session).reanalyze(
                account_external_id=account.external_id,
                direction_name=direction.name,
            )
            result = next(item for item in reanalyzed if item.vacancy.id == duplicate.id)

            assert result.evaluation.category is RuleCategory.REJECTED
            assert result.state is VacancyState.FILTERED_OUT
            assert vacancies.get(duplicate.id).duplicate_of_id == canonical.id
            assert (
                ApplicationAutomationService(session)
                .prepare_for_account_id(
                    account_id=account.id,
                    direction_name=direction.name,
                    include_stretch=False,
                )
                .created
                == 0
            )
            assert session.scalar(select(func.count(ApplicationModel.id))) == 1
    finally:
        database.close()


def test_relinking_canonical_vacancy_keeps_duplicate_family_flat(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            repository = VacancyRepository(session)
            title = "Python backend разработчик"
            first = repository.upsert(detailed_vacancy("duplicate-family-1", title))
            second = repository.upsert(detailed_vacancy("duplicate-family-2", title))
            third = repository.upsert(detailed_vacancy("duplicate-family-3", title))
            new_canonical = repository.upsert(detailed_vacancy("duplicate-family-4", title))

            repository.mark_duplicate(second.id, first.id, 0.99)
            repository.mark_duplicate(third.id, first.id, 0.99)
            repository.mark_duplicate(first.id, new_canonical.id, 0.99)

            expected_family = tuple(sorted((first.id, second.id, third.id, new_canonical.id)))
            assert repository.get(first.id).duplicate_of_id == new_canonical.id
            assert repository.get(second.id).duplicate_of_id == new_canonical.id
            assert repository.get(third.id).duplicate_of_id == new_canonical.id
            assert repository.get(new_canonical.id).duplicate_of_id is None
            for vacancy_id in expected_family:
                assert repository.duplicate_family_ids(vacancy_id) == expected_family

            with pytest.raises(ValueError, match="duplicate cycle"):
                repository.mark_duplicate(new_canonical.id, second.id, 0.99)
    finally:
        database.close()


def test_fullstack_found_by_python_is_routed_to_it_without_duplicate_task(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тест", "account-routing")
            resume = ResumeRepository(session).upsert(account.id, "resume-routing", "Python")
            directions = DirectionRepository(session)
            python_direction = directions.create(
                account.id,
                "Python backend",
                scoring_config={"role_scope": DirectionScope.PYTHON_BACKEND.value},
            )
            it_direction = directions.create(
                account.id,
                "ИТ",
                scoring_config={"role_scope": DirectionScope.IT_ADJACENT.value},
            )
            directions.attach_resume(python_direction.id, resume.id)
            directions.attach_resume(it_direction.id, resume.id)
            vacancy = detailed_vacancy(
                "routed-fullstack",
                "Middle Full-stack разработчик",
                description="Python, FastAPI, TypeScript и React",
            )

            analyzed = VacancyAnalysisService(session).synchronize(
                account_external_id="account-routing",
                direction_name="Python backend",
                vacancies=(vacancy,),
            )

            assert analyzed[0].evaluation.category is RuleCategory.ROUTED
            stored = VacancyRepository(session).get_by_hh_id("routed-fullstack")
            assert stored is not None
            assert (
                directions.get_tracked_vacancy(python_direction.id, stored.id).state
                is VacancyState.SKIPPED
            )
            assert (
                directions.get_tracked_vacancy(it_direction.id, stored.id).state
                is VacancyState.ANALYZED
            )

            prepared = ApplicationAutomationService(session).prepare(
                account_external_id="account-routing",
                direction_name="ИТ",
                include_stretch=True,
            )
            repeated = ApplicationAutomationService(session).prepare(
                account_external_id="account-routing",
                direction_name="ИТ",
                include_stretch=True,
            )

            assert prepared.created == 1
            assert repeated.created == 0
            assert repeated.existing == 1
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 1
            assert (
                directions.get_tracked_vacancy(it_direction.id, stored.id).state
                is VacancyState.QUEUED
            )

            VacancyAnalysisService(session).reanalyze(
                account_external_id="account-routing",
                direction_name="Python backend",
            )

            assert (
                directions.get_tracked_vacancy(it_direction.id, stored.id).state
                is VacancyState.QUEUED
            )
    finally:
        database.close()
