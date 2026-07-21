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
                state=VacancyState.ANALYZED,
                score=80,
                details={"category": "MATCH", "accepted": True},
            )
            directions.apply_rules(
                direction.id,
                stretch.id,
                state=VacancyState.ANALYZED,
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
            needs_input = service.record_result(
                first,
                HhApplyResult(
                    HhApplyStatus.QUESTIONS_REQUIRED,
                    first.vacancy.source_url,
                    questions=("Личный вопрос",),
                ),
            )
            assert not needs_input.sent
            assert (
                ApplicationRepository(session).get(first.application.id).state
                is ApplicationState.APPLYING
            )
            assert QueueTaskRepository(session).get(first.task.id).state is TaskState.INPUT_REQUIRED

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
                state=VacancyState.ANALYZED,
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
            uncertain = service.record_result(
                uncertain_job,
                HhApplyResult(HhApplyStatus.UNKNOWN_RESULT, uncertain_vacancy.source_url),
            )
            assert not uncertain.blocking
            assert (
                QueueTaskRepository(session).get(uncertain_job.task.id).state
                is TaskState.UNKNOWN_RESULT
            )
            assert SystemStateRepository(session).get().state is SystemState.RUNNING
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

            closed_vacancy = vacancies.upsert(
                VacancyData("400", "Closed Python role", "https://hh.ru/vacancy/400")
            )
            directions.track_vacancy(direction.id, closed_vacancy.id)
            closed_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                closed_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(closed_application.id, 40)
            closed_job = service.claim_next(direction.id)
            assert closed_job is not None
            closed = service.record_result(
                closed_job,
                HhApplyResult(HhApplyStatus.VACANCY_CLOSED, closed_vacancy.source_url),
            )
            assert not closed.sent
            assert (
                ApplicationRepository(session).get(closed_application.id).state
                is ApplicationState.CLOSED
            )
            assert QueueTaskRepository(session).get(closed_job.task.id).state is TaskState.SKIPPED

            auth_vacancy = vacancies.upsert(
                VacancyData("500", "Protected Python role", "https://hh.ru/vacancy/500")
            )
            directions.track_vacancy(direction.id, auth_vacancy.id)
            auth_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                auth_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(auth_application.id, 30)
            auth_job = service.claim_next(direction.id)
            assert auth_job is not None
            auth_required = service.record_result(
                auth_job,
                HhApplyResult(HhApplyStatus.AUTH_REQUIRED, auth_vacancy.source_url),
            )
            assert auth_required.blocking
            assert SystemStateRepository(session).get().state is SystemState.AUTH_REQUIRED
            service.resume_after_authentication()
            assert SystemStateRepository(session).get().state is SystemState.RUNNING
    finally:
        database.close()
