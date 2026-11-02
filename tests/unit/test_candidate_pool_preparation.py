import pytest
from sqlalchemy import delete, func, select

from hugin.core.settings import Settings, get_settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    DirectionVacancyModel,
)
from hugin.domain.directions import VacancyState
from hugin.repositories.directions import DirectionRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.application_selection_gate import ApplicationSelectionGate
from hugin.services.vacancy_analysis import RULES_VERSION
from tests.unit.test_application_candidate_pool import pool_jobs


def test_analyzed_candidates_join_the_ranking_before_any_letter(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUGIN_APPLICATION_POOL_TARGET", "40")
    monkeypatch.setenv("HUGIN_APPLICATION_POOL_MINIMUM", "30")
    get_settings.cache_clear()
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 40)
            account_id = jobs[0].application.account_id
            session.execute(delete(ApplicationTaskModel))
            session.execute(delete(ApplicationEventModel))
            session.execute(delete(ApplicationModel))
            for row in session.scalars(select(DirectionVacancyModel)):
                row.state = VacancyState.ANALYZED
            session.flush()
            service = ApplicationAutomationService(session)
            service.prepare_vacancies_for_account_id(
                account_id=account_id,
                vacancy_ids=tuple(job.vacancy.id for job in jobs[-3:]),
                include_stretch=True,
            )
            gate = ApplicationSelectionGate(session)
            assert gate.blocking_reason(account_id, progressive=True) is None
            chosen = service.claim_next(account_id=account_id)
            assert chosen is not None and chosen.vacancy.id == jobs[0].vacancy.id
            assert session.scalar(select(func.count()).select_from(ApplicationTaskModel)) == 40
    finally:
        database.close()


def test_preparation_uses_the_currently_allowed_direction(
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
            session.execute(
                delete(ApplicationTaskModel).where(ApplicationTaskModel.id == strongest.task.id)
            )
            session.execute(
                delete(ApplicationEventModel).where(
                    ApplicationEventModel.application_id == strongest.application.id
                )
            )
            session.execute(
                delete(ApplicationModel).where(ApplicationModel.id == strongest.application.id)
            )
            directions = DirectionRepository(session)
            stale = directions.create(strongest.application.account_id, "Stale higher score")
            directions.attach_resume(stale.id, strongest.resume.id)
            directions.track_vacancy(stale.id, strongest.vacancy.id)
            directions.apply_rules(
                stale.id,
                strongest.vacancy.id,
                state=VacancyState.ANALYZED,
                score=100,
                details={"category": "MATCH", "fit_tier": 1},
                rules_version=RULES_VERSION,
            )
            monkeypatch.setattr(
                ApplicationAutomationService,
                "_semantic_selection_current",
                lambda self, direction, vacancy, resume_id: direction.id != stale.id,
            )
            chosen = ApplicationAutomationService(session).claim_next(
                account_id=strongest.application.account_id
            )
            assert chosen is not None and chosen.vacancy.id == strongest.vacancy.id
            assert chosen.application.direction_id == strongest.application.direction_id
    finally:
        database.close()
