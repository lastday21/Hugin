from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain import (
    ApplicationReconciliationResult,
    ApplicationState,
    HhApplyResult,
    HhApplyStatus,
    ReconciliationStatus,
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
from hugin.services.application_reconciliation import ApplicationReconciliationService
from hugin.services.queue import QueueService
from hugin.services.vacancy_analysis import RULES_VERSION

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
                rules_version=RULES_VERSION,
            )
            directions.apply_rules(
                direction.id,
                stretch.id,
                state=VacancyState.ANALYZED,
                score=60,
                details={"category": "STRETCH", "accepted": True},
                rules_version=RULES_VERSION,
            )

            service = ApplicationAutomationService(session)
            prepared = service.prepare(
                account_external_id="account-1",
                direction_name="Python backend",
                include_stretch=True,
            )
            assert prepared.created == 2
            assert prepared.resume == resume
            repeated = service.prepare_for_account_id(
                account_id=account.id,
                direction_name="Python backend",
                include_stretch=True,
            )
            assert repeated.created == 0
            assert repeated.existing == 2
            assert service.recover_interrupted() == 0
            SystemStateRepository(session).transition(SystemState.RUNNING)

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
                apply_delay=timedelta(seconds=45),
            )
            assert recorded.sent
            assert recorded.next_apply_at is not None
            assert (
                ApplicationRepository(session).get(second.application.id).state
                is ApplicationState.APPLIED
            )
            assert QueueTaskRepository(session).get(second.task.id).state is TaskState.COMPLETED
            assert service.applied_since(account.id, datetime(2026, 1, 1, tzinfo=UTC)) == 1
            SystemStateRepository(session).set_next_apply_at(None)

            already_vacancy = vacancies.upsert(
                VacancyData("already", "Python", "https://hh.ru/vacancy/already")
            )
            directions.track_vacancy(direction.id, already_vacancy.id)
            directions.apply_rules(
                direction.id,
                already_vacancy.id,
                state=VacancyState.QUEUED,
                score=55,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            already_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                already_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(already_application.id, 55)
            already_job = service.claim_next(direction.id)
            assert already_job is not None
            already = service.record_result(
                already_job,
                HhApplyResult(HhApplyStatus.ALREADY_APPLIED, already_vacancy.source_url),
            )
            assert not already.sent
            assert service.applied_since(account.id, datetime(2026, 1, 1, tzinfo=UTC)) == 1

            uncertain_vacancy = vacancies.upsert(
                VacancyData("300", "Python engineer", "https://hh.ru/vacancy/300")
            )
            directions.track_vacancy(direction.id, uncertain_vacancy.id)
            directions.apply_rules(
                direction.id,
                uncertain_vacancy.id,
                state=VacancyState.QUEUED,
                score=50,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
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
            assert uncertain.blocking
            assert (
                QueueTaskRepository(session).get(uncertain_job.task.id).state
                is TaskState.UNKNOWN_RESULT
            )
            assert SystemStateRepository(session).get().state is SystemState.PAUSED
            unknown_event = ApplicationRepository(session).list_events(
                uncertain_job.application.id
            )[-1]
            assert unknown_event.payload["final_url"] == uncertain_vacancy.source_url

            confirmed = ApplicationReconciliationService(session).reconcile(
                uncertain_job.task.id,
                ApplicationReconciliationResult(
                    ReconciliationStatus.APPLIED,
                    final_url="https://hh.ru/applicant/negotiations",
                    confirmation="Найдено в списке откликов",
                ),
            )
            assert not confirmed.blocking
            assert (
                ApplicationRepository(session).get(uncertain_job.application.id).state
                is ApplicationState.APPLIED
            )
            assert (
                QueueTaskRepository(session).get(uncertain_job.task.id).state is TaskState.COMPLETED
            )
            assert SystemStateRepository(session).get().state is SystemState.PAUSED
            QueueService(session).resume()
            assert SystemStateRepository(session).get().state is SystemState.RUNNING

            closed_vacancy = vacancies.upsert(
                VacancyData("400", "Closed Python role", "https://hh.ru/vacancy/400")
            )
            directions.track_vacancy(direction.id, closed_vacancy.id)
            directions.apply_rules(
                direction.id,
                closed_vacancy.id,
                state=VacancyState.QUEUED,
                score=40,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
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
            directions.apply_rules(
                direction.id,
                auth_vacancy.id,
                state=VacancyState.QUEUED,
                score=30,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
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
            assert SystemStateRepository(session).get().state is SystemState.PAUSED
    finally:
        database.close()


def test_retry_after_schedules_task_and_all_new_applications(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC) - timedelta(seconds=1)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "retry-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            direction = DirectionRepository(session).create(account.id, "ИТ")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("retry", "Python", "https://hh.ru/vacancy/retry")
            )
            directions = DirectionRepository(session)
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=50,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(application.id, 50, now)
            service = ApplicationAutomationService(session)
            SystemStateRepository(session).transition(SystemState.RUNNING)
            job = service.claim_next(direction.id)
            assert job is not None

            recorded = service.record_result(
                job,
                HhApplyResult(
                    HhApplyStatus.RETRYABLE_ERROR,
                    vacancy.source_url,
                    retry_after_seconds=120,
                ),
                now=now,
            )

            expected = now + timedelta(seconds=120)
            assert recorded.next_apply_at == expected
            assert QueueTaskRepository(session).get(job.task.id).scheduled_at == expected
            assert SystemStateRepository(session).get().next_apply_at == expected
    finally:
        database.close()


def test_rule_change_skips_and_can_restore_pending_task(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "rules-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-rules", "Python")
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData("rules", "Python backend", "https://hh.ru/vacancy/rules")
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            service = ApplicationAutomationService(session)
            assert (
                service.prepare_for_account_id(
                    account_id=account.id,
                    direction_name=direction.name,
                    include_stretch=True,
                ).created
                == 1
            )
            application = ApplicationRepository(session).get_by_key(
                account.id,
                vacancy.id,
                resume.id,
            )
            assert application is not None
            task = QueueTaskRepository(session).get_by_application_id(application.id)
            assert task is not None

            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.FILTERED_OUT,
                score=0,
                details={"category": "REJECTED", "accepted": False},
                rules_version=RULES_VERSION,
            )
            service.prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=True,
            )
            skipped = QueueTaskRepository(session).get(task.id)
            assert skipped.state is TaskState.SKIPPED
            assert skipped.last_error_code == "VACANCY_RULES_CHANGED"

            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=75,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            restored = service.prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=True,
            )

            assert restored.created == 1
            assert QueueTaskRepository(session).get(task.id).state is TaskState.PENDING
    finally:
        database.close()


def test_review_claim_is_allowed_only_while_queue_is_paused(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = ApplicationAutomationService(session)

            assert service.claim_next(allow_paused_review=True) is None

            SystemStateRepository(session).transition(SystemState.RUNNING)
            with pytest.raises(RuntimeError, match="только на паузе"):
                service.claim_next(allow_paused_review=True)
    finally:
        database.close()
