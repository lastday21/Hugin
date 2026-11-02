import pytest
from sqlalchemy import select

from hugin.core.settings import Settings, get_settings
from hugin.database import create_database
from hugin.database.models import ApplicationModel, ApplicationTaskModel, CareerDirectionModel
from hugin.domain.directions import VacancyState
from hugin.domain.tasks import TaskState
from hugin.repositories.directions import DirectionRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.vacancy_analysis import RULES_VERSION
from tests.unit.test_application_candidate_pool import pool_jobs


def test_inactive_direction_task_is_reconciled_before_pool_ranking(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUGIN_APPLICATION_POOL_TARGET", "40")
    monkeypatch.setenv("HUGIN_APPLICATION_POOL_MINIMUM", "30")
    get_settings.cache_clear()
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 40)
            strongest = jobs[0]
            directions = DirectionRepository(session)
            old = directions.create(strongest.application.account_id, "Inactive old direction")
            directions.attach_resume(old.id, strongest.resume.id)
            directions.track_vacancy(old.id, strongest.vacancy.id)
            directions.apply_rules(
                old.id,
                strongest.vacancy.id,
                state=VacancyState.QUEUED,
                score=100,
                details={"category": "MATCH", "fit_tier": 1},
                rules_version=RULES_VERSION,
            )
            model = session.get(CareerDirectionModel, old.id)
            assert model is not None
            model.is_active = False
            application = session.scalar(
                select(ApplicationModel).where(ApplicationModel.id == strongest.application.id)
            )
            assert application is not None
            application.direction_id = old.id
            session.flush()
            chosen = ApplicationAutomationService(session).claim_next(
                account_id=strongest.application.account_id
            )
            assert chosen is not None and chosen.vacancy.id == strongest.vacancy.id
            assert chosen.application.direction_id == strongest.application.direction_id
    finally:
        database.close()


def test_currently_allowed_selection_recovery_counts_towards_the_pool(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUGIN_APPLICATION_POOL_TARGET", "40")
    monkeypatch.setenv("HUGIN_APPLICATION_POOL_MINIMUM", "30")
    get_settings.cache_clear()
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 40)
            strongest = jobs[0]
            task = session.get(ApplicationTaskModel, strongest.task.id)
            assert task is not None
            task.state = TaskState.SKIPPED
            task.last_error_code = "VACANCY_RULES_CHANGED"
            session.flush()
            chosen = ApplicationAutomationService(session).claim_next(
                account_id=strongest.application.account_id
            )
            assert chosen is not None and chosen.vacancy.id == strongest.vacancy.id
    finally:
        database.close()
