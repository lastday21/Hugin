from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationModel, ApplicationTaskModel, SystemStateModel
from hugin.domain import (
    ApplicationEventType,
    ApplicationState,
    HhApplyResult,
    HhApplyStatus,
    SystemState,
    TaskState,
    VacancyData,
    VacancyState,
)
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    ResumeRepository,
    SystemStateRepository,
    VacancyRepository,
)
from hugin.services.application_automation import ApplicationAutomationService, ApplyJob
from hugin.services.vacancy_analysis import RULES_VERSION

pytestmark = pytest.mark.integration


def queued_jobs(session: Session, count: int = 2) -> tuple[ApplyJob, ...]:
    account = AccountRepository(session).create("Границы заданий")
    resume = ResumeRepository(session).upsert(account.id, "boundary-resume", "Python")
    directions = DirectionRepository(session)
    direction = directions.create(account.id, "Python backend")
    directions.attach_resume(direction.id, resume.id)
    jobs: list[ApplyJob] = []
    for index in range(count):
        vacancy = VacancyRepository(session).upsert(
            VacancyData(
                f"boundary-{index}",
                "Python developer",
                f"https://hh.ru/vacancy/boundary-{index}",
            )
        )
        directions.track_vacancy(direction.id, vacancy.id)
        tracked = directions.apply_rules(
            direction.id,
            vacancy.id,
            state=VacancyState.QUEUED,
            score=90 - index,
            details={"category": "MATCH", "accepted": True, "fit_tier": 1},
            rules_version=RULES_VERSION,
        )
        application = ApplicationRepository(session).create_apply_intent(
            account.id, vacancy.id, resume.id, direction.id
        )
        task = QueueTaskRepository(session).enqueue(application.id, 90 - index)
        jobs.append(ApplyJob(task, application, vacancy, resume, tracked))
    SystemStateRepository(session).transition(SystemState.RUNNING)
    return tuple(jobs)


@pytest.mark.parametrize(
    "status",
    [
        HhApplyStatus.APPLIED,
        HhApplyStatus.ALREADY_APPLIED,
        HhApplyStatus.VACANCY_CLOSED,
        HhApplyStatus.UNKNOWN_RESULT,
        HhApplyStatus.RETRYABLE_ERROR,
    ],
)
def test_result_cannot_change_a_task_from_another_application(
    settings: Settings, status: HhApplyStatus
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            queued = queued_jobs(session)
            service = ApplicationAutomationService(session)
            first = service.claim_next(account_id=queued[0].application.account_id)
            second = service.claim_next(account_id=queued[0].application.account_id)
            assert first is not None and second is not None
        with pytest.raises((ValueError, RuntimeError)), database.sessions.begin() as session:
            ApplicationAutomationService(session).record_result(
                replace(first, task=second.task),
                HhApplyResult(status, first.vacancy.source_url),
            )
        with database.sessions() as session:
            for job in (first, second):
                assert ApplicationRepository(session).get(job.application.id).state is (
                    ApplicationState.APPLYING
                )
                assert QueueTaskRepository(session).get(job.task.id).state is TaskState.RUNNING
    finally:
        database.close()


@pytest.mark.parametrize("stale", ["released", "wrong_application", "wrong_status"])
def test_stale_or_mismatched_form_cannot_change_the_saved_task(
    settings: Settings, stale: str
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            queued = queued_jobs(session)
            service = ApplicationAutomationService(session)
            job = service.claim_next_form_preflight(account_id=queued[0].application.account_id)
            assert job is not None
            if stale == "released":
                service.release_form_preflight(job)
            expected = QueueTaskRepository(session).get(job.task.id).state
        with pytest.raises((ValueError, RuntimeError)), database.sessions.begin() as session:
            ApplicationAutomationService(session).record_form_preflight(
                replace(job, application=queued[1].application)
                if stale == "wrong_application"
                else job,
                HhApplyResult(
                    HhApplyStatus.APPLIED
                    if stale == "wrong_status"
                    else HhApplyStatus.QUESTIONS_REQUIRED,
                    job.vacancy.source_url,
                    questions=("Когда готовы приступить?",),
                ),
                now=datetime.now(UTC),
            )
        with database.sessions() as session:
            assert QueueTaskRepository(session).get(job.task.id).state is expected
    finally:
        database.close()


@pytest.mark.parametrize(
    "changed", ["account", "vacancy", "resume", "resume_account", "future_attempt", "no_attempt"]
)
def test_application_result_rejects_wrong_identity_or_unregistered_attempt(
    settings: Settings, changed: str
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            queued = queued_jobs(session)
            job = ApplicationAutomationService(session).claim_next()
            assert job is not None
        if changed == "account":
            invalid = replace(job, application=replace(job.application, account_id=999))
        elif changed == "vacancy":
            invalid = replace(job, vacancy=queued[1].vacancy)
        elif changed == "resume":
            invalid = replace(job, resume=replace(job.resume, id=999))
        elif changed == "resume_account":
            invalid = replace(job, resume=replace(job.resume, account_id=999))
        else:
            invalid = replace(
                job, task=replace(job.task, attempts=9 if changed == "future_attempt" else 0)
            )
        with pytest.raises(ValueError), database.sessions.begin() as session:
            ApplicationAutomationService(session).record_result(
                invalid, HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url)
            )
        with database.sessions() as session:
            assert QueueTaskRepository(session).get(job.task.id).state is TaskState.RUNNING
            assert (
                ApplicationRepository(session).get(job.application.id).state
                is ApplicationState.APPLYING
            )
    finally:
        database.close()


def test_result_with_a_deleted_task_does_not_invent_a_confirmation(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            queued_jobs(session, 1)
            job = ApplicationAutomationService(session).claim_next()
            assert job is not None
            task = session.get(ApplicationTaskModel, job.task.id)
            assert task is not None
            session.delete(task)
        with pytest.raises(LookupError), database.sessions.begin() as session:
            ApplicationAutomationService(session).record_result(
                job, HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url)
            )
        with database.sessions() as session:
            assert (
                ApplicationRepository(session).get(job.application.id).state
                is ApplicationState.APPLYING
            )
    finally:
        database.close()


def two_attempts(session: Session) -> tuple[ApplyJob, ApplyJob]:
    queued_jobs(session, 1)
    service = ApplicationAutomationService(session)
    first = service.claim_next()
    assert first is not None
    service.record_result(
        first,
        HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, first.vacancy.source_url),
        retry_delay=timedelta(0),
    )
    second = service.claim_next()
    assert second is not None
    assert first.task.attempts == 1 and second.task.attempts == 2
    return first, second


@pytest.mark.parametrize("status", [HhApplyStatus.RETRYABLE_ERROR, HhApplyStatus.UNKNOWN_RESULT])
def test_old_failure_does_not_rewrite_the_current_attempt(
    settings: Settings, status: HhApplyStatus
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            first, second = two_attempts(session)
        with pytest.raises(RuntimeError), database.sessions.begin() as session:
            ApplicationAutomationService(session).record_result(
                first, HhApplyResult(status, first.vacancy.source_url)
            )
        with database.sessions() as session:
            task = QueueTaskRepository(session).get(second.task.id)
            assert task.state is TaskState.RUNNING and task.attempts == 2
    finally:
        database.close()


@pytest.mark.parametrize("status", [HhApplyStatus.APPLIED, HhApplyStatus.ALREADY_APPLIED])
def test_late_confirmation_is_saved_once_without_completing_the_new_attempt(
    settings: Settings, status: HhApplyStatus
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            first, second = two_attempts(session)
        for _ in range(2):
            with database.sessions.begin() as session:
                recorded = ApplicationAutomationService(session).record_result(
                    first, HhApplyResult(status, first.vacancy.source_url)
                )
                assert recorded.sent is (status is HhApplyStatus.APPLIED)
        with database.sessions.begin() as session:
            tasks = QueueTaskRepository(session)
            assert tasks.get(second.task.id).state is TaskState.UNKNOWN_RESULT
            assert tasks.get(second.task.id).last_error_code == "PREVIOUS_ATTEMPT_CONFIRMED"
            applications = ApplicationRepository(session)
            assert applications.get(first.application.id).state is ApplicationState.APPLIED
            confirmations = [
                event
                for event in applications.list_events(first.application.id)
                if event.event_type is ApplicationEventType.APPLIED
            ]
            assert len(confirmations) == 1
            assert confirmations[0].payload["attempt_number"] == 1
            assert confirmations[0].payload["current_attempt_number"] == 2
            assert confirmations[0].payload["stale_attempt"] is True
            service = ApplicationAutomationService(session)
            assert service.claim_next() is None
            service.record_result(
                second, HhApplyResult(HhApplyStatus.APPLIED, second.vacancy.source_url)
            )
            assert tasks.get(second.task.id).state is TaskState.COMPLETED
            confirmations = [
                event
                for event in applications.list_events(first.application.id)
                if event.event_type is ApplicationEventType.APPLIED
            ]
            assert len(confirmations) == 2
            assert {event.payload["attempt_number"] for event in confirmations} == {1, 2}
    finally:
        database.close()


@pytest.mark.parametrize("revoked_state", [TaskState.SKIPPED, TaskState.RETRY_SCHEDULED])
def test_actual_confirmation_survives_revocation_without_reopening_the_task(
    settings: Settings, revoked_state: TaskState
) -> None:
    database = create_database(settings)
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            queued_jobs(session, 1)
            service = ApplicationAutomationService(session)
            job = service.claim_next()
            assert job is not None
            QueueTaskRepository(session).transition(job.task.id, revoked_state, scheduled_at=now)
            SystemStateRepository(session).set_next_apply_at(now + timedelta(minutes=2))
        with database.sessions.begin() as session:
            recorded = ApplicationAutomationService(session).record_result(
                job,
                HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url),
                now=now,
                apply_delay=timedelta(seconds=30),
            )
            assert recorded.sent
            assert recorded.next_apply_at == now + timedelta(minutes=2)
        with database.sessions() as session:
            assert QueueTaskRepository(session).get(job.task.id).state is TaskState.SKIPPED
            applications = ApplicationRepository(session)
            assert applications.get(job.application.id).state is ApplicationState.APPLIED
            confirmations = [
                event
                for event in applications.list_events(job.application.id)
                if event.event_type is ApplicationEventType.APPLIED
            ]
            assert len(confirmations) == 1
            assert confirmations[0].payload["late_confirmation"] is True
            assert confirmations[0].payload["stale_attempt"] is False
            assert confirmations[0].payload["attempt_number"] == 1
    finally:
        database.close()


def test_result_refresh_keeps_pending_changes_in_the_same_transaction(settings: Settings) -> None:
    database = create_database(settings)
    next_apply_at = datetime.now(UTC) + timedelta(minutes=5)
    try:
        with database.sessions.begin() as session:
            queued_jobs(session, 1)
            job = ApplicationAutomationService(session).claim_next()
            assert job is not None
        with database.sessions.begin() as session:
            assert not session.autoflush
            application = session.get(ApplicationModel, job.application.id)
            task = session.get(ApplicationTaskModel, job.task.id)
            system = session.get(SystemStateModel, 1)
            assert application is not None and task is not None and system is not None
            application.state = ApplicationState.INVITED
            task.state = TaskState.SKIPPED
            system.next_apply_at = next_apply_at
            ApplicationAutomationService(session).record_result(
                job, HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url)
            )
        with database.sessions() as session:
            assert ApplicationRepository(session).get(job.application.id).state is (
                ApplicationState.INVITED
            )
            assert QueueTaskRepository(session).get(job.task.id).state is TaskState.SKIPPED
            assert SystemStateRepository(session).get().next_apply_at == next_apply_at
            confirmations = [
                event
                for event in ApplicationRepository(session).list_events(job.application.id)
                if event.event_type is ApplicationEventType.APPLIED
            ]
            assert len(confirmations) == 1 and confirmations[0].payload["late_confirmation"]
    finally:
        database.close()
