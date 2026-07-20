from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain import (
    ApplicationState,
    HhApplyResult,
    HhApplyStatus,
    SystemState,
    TaskState,
    VacancyData,
)
from hugin.domain.directions import VacancyState
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    ResumeRepository,
    SystemStateRepository,
    VacancyRepository,
)
from hugin.services.application_automation import ApplicationAutomationService

pytestmark = pytest.mark.integration


def test_automation_prepares_claims_and_records_results(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "account-1")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-1",
                "Python backend разработчик",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancies = VacancyRepository(session)
            match = vacancies.upsert(
                VacancyData("100", "Python developer", "https://hh.ru/vacancy/100")
            )
            stretch = vacancies.upsert(
                VacancyData("200", "AI Agent Engineer", "https://hh.ru/vacancy/200")
            )
            directions.track_vacancy(direction.id, match.id)
            directions.track_vacancy(direction.id, stretch.id)
            directions.apply_rules(
                direction.id,
                match.id,
                state=VacancyState.FILTERED,
                score=80,
                details={"category": "MATCH", "accepted": True},
            )
            directions.apply_rules(
                direction.id,
                stretch.id,
                state=VacancyState.FILTERED,
                score=60,
                details={"category": "STRETCH", "accepted": True},
            )

            service = ApplicationAutomationService(session)
            prepared = service.prepare(
                account_external_id="account-1",
                direction_name="Python backend",
                include_stretch=True,
            )
            assert prepared.created == 2
            assert prepared.resume == resume

            first = service.claim_next(direction.id)
            assert first is not None
            assert first.vacancy.hh_id == "100"
            skipped = service.record_result(
                first,
                HhApplyResult(
                    HhApplyStatus.QUESTIONS_REQUIRED,
                    first.vacancy.source_url,
                    questions=("Личный вопрос",),
                ),
            )
            assert not skipped.sent
            assert (
                ApplicationRepository(session).get(first.application.id).state
                is ApplicationState.CLOSED
            )
            assert QueueTaskRepository(session).get(first.task.id).state is TaskState.SKIPPED

            second = service.claim_next(direction.id)
            assert second is not None
            assert second.vacancy.hh_id == "200"
            recorded = service.record_result(
                second,
                HhApplyResult(HhApplyStatus.APPLIED, second.vacancy.source_url, "успешно"),
            )
            assert recorded.sent
            assert (
                ApplicationRepository(session).get(second.application.id).state
                is ApplicationState.APPLIED
            )
            assert QueueTaskRepository(session).get(second.task.id).state is TaskState.COMPLETED
            assert service.applied_since(account.id, datetime(2026, 1, 1, tzinfo=UTC)) == 1

            uncertain_vacancy = vacancies.upsert(
                VacancyData("300", "Python engineer", "https://hh.ru/vacancy/300")
            )
            directions.track_vacancy(direction.id, uncertain_vacancy.id)
            directions.apply_rules(
                direction.id,
                uncertain_vacancy.id,
                state=VacancyState.FILTERED,
                score=50,
                details={"category": "MATCH", "accepted": True},
            )
            uncertain_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                uncertain_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(uncertain_application.id, 50)
            uncertain_job = service.claim_next(direction.id)
            assert uncertain_job is not None
            blocked = service.record_result(
                uncertain_job,
                HhApplyResult(HhApplyStatus.UNKNOWN_RESULT, uncertain_vacancy.source_url),
            )
            assert blocked.blocking
            assert (
                QueueTaskRepository(session).get(uncertain_job.task.id).state
                is TaskState.UNKNOWN_RESULT
            )
            assert SystemStateRepository(session).get().state is SystemState.PAUSED
            unknown_event = ApplicationRepository(session).list_events(
                uncertain_job.application.id
            )[-1]
            assert unknown_event.payload["final_url"] == uncertain_vacancy.source_url

            confirmed = service.confirm_unknown_as_applied(
                uncertain_job.task.id,
                final_url="https://hh.ru/applicant/negotiations",
                confirmation="Найдено в списке откликов",
            )
            assert confirmed.sent
            assert (
                ApplicationRepository(session).get(uncertain_job.application.id).state
                is ApplicationState.APPLIED
            )
            assert (
                QueueTaskRepository(session).get(uncertain_job.task.id).state is TaskState.COMPLETED
            )
            assert SystemStateRepository(session).get().state is SystemState.RUNNING
    finally:
        database.close()
