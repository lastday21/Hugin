from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationModel, ApplicationTaskModel
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


def create_claim(session: Session, suffix: str = "one") -> ApplyJob:
    account = AccountRepository(session).create(f"Independent result {suffix}")
    resume = ResumeRepository(session).upsert(account.id, f"resume-{suffix}", "Python developer")
    directions = DirectionRepository(session)
    direction = directions.create(account.id, "Python backend")
    directions.attach_resume(direction.id, resume.id)
    vacancy = VacancyRepository(session).upsert(
        VacancyData(
            f"result-{suffix}", "Python developer", f"https://hh.ru/vacancy/result-{suffix}"
        )
    )
    directions.track_vacancy(direction.id, vacancy.id)
    directions.apply_rules(
        direction.id,
        vacancy.id,
        state=VacancyState.QUEUED,
        score=95,
        details={"category": "MATCH", "accepted": True, "fit_tier": 1},
        rules_version=RULES_VERSION,
    )
    application = ApplicationRepository(session).create_apply_intent(
        account.id, vacancy.id, resume.id, direction.id
    )
    QueueTaskRepository(session).enqueue(application.id, 95)
    system = SystemStateRepository(session)
    if system.get().state is not SystemState.RUNNING:
        system.transition(SystemState.RUNNING)
    job = ApplicationAutomationService(session).claim_next(account_id=account.id)
    assert job is not None and job.task.attempts == 1
    return job


def next_attempt(session: Session, previous: ApplyJob) -> ApplyJob:
    service = ApplicationAutomationService(session)
    service.record_result(
        previous,
        HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, previous.vacancy.source_url),
        retry_delay=timedelta(0),
    )
    current = service.claim_next(account_id=previous.application.account_id)
    assert current is not None and current.task.attempts == previous.task.attempts + 1
    return current


@pytest.mark.parametrize("status", [HhApplyStatus.APPLIED, HhApplyStatus.ALREADY_APPLIED])
def test_result_lock_refreshes_a_task_loaded_before_another_session_claimed_it(
    settings: Settings, status: HhApplyStatus
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            original = create_claim(session)
        with database.sessions.begin() as earlier_session:
            cached_task = earlier_session.get(ApplicationTaskModel, original.task.id)
            cached_application = earlier_session.get(ApplicationModel, original.application.id)
            assert cached_task is not None and cached_application is not None
            assert cached_task.attempts == 1
            with database.sessions.begin() as later_session:
                current = next_attempt(later_session, original)
            assert cached_task.attempts == 1
            ApplicationAutomationService(earlier_session).record_result(
                original, HhApplyResult(status, original.vacancy.source_url)
            )
        with database.sessions() as session:
            current_task = QueueTaskRepository(session).get(current.task.id)
            assert current_task.attempts == 2
            assert current_task.state is TaskState.UNKNOWN_RESULT
            confirmations = [
                event
                for event in ApplicationRepository(session).list_events(original.application.id)
                if event.event_type is ApplicationEventType.APPLIED
            ]
            assert len(confirmations) == 1
            assert confirmations[0].payload["attempt_number"] == 1
    finally:
        database.close()


def test_cached_old_failure_cannot_reschedule_a_new_attempt(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            original = create_claim(session)
        with database.sessions() as earlier_session:
            cached_task = earlier_session.get(ApplicationTaskModel, original.task.id)
            cached_application = earlier_session.get(ApplicationModel, original.application.id)
            assert cached_task is not None and cached_application is not None
            with database.sessions.begin() as later_session:
                current = next_attempt(later_session, original)
            assert cached_task.attempts == 1
            with pytest.raises(RuntimeError):
                ApplicationAutomationService(earlier_session).record_result(
                    original,
                    HhApplyResult(HhApplyStatus.AUTH_REQUIRED, original.vacancy.source_url),
                )
            earlier_session.rollback()
        with database.sessions() as session:
            assert QueueTaskRepository(session).get(current.task.id).state is TaskState.RUNNING
            assert SystemStateRepository(session).get().state is SystemState.RUNNING
    finally:
        database.close()


@pytest.mark.parametrize("changed", ["vacancy_public_id", "resume_public_id", "vacancy_url"])
def test_public_identity_cannot_be_replaced_while_internal_identifiers_stay_valid(
    settings: Settings, changed: str
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            original = create_claim(session)
        if changed == "vacancy_public_id":
            forged = replace(
                original, vacancy=replace(original.vacancy, hh_id="another-public-vacancy")
            )
        elif changed == "resume_public_id":
            forged = replace(
                original, resume=replace(original.resume, hh_id="another-public-resume")
            )
        else:
            forged = replace(
                original,
                vacancy=replace(
                    original.vacancy, source_url="https://hh.ru/vacancy/another-public-vacancy"
                ),
            )
        with pytest.raises(ValueError), database.sessions.begin() as session:
            ApplicationAutomationService(session).record_result(
                forged, HhApplyResult(HhApplyStatus.APPLIED, forged.vacancy.source_url)
            )
        with database.sessions() as session:
            assert QueueTaskRepository(session).get(original.task.id).state is TaskState.RUNNING
            assert (
                ApplicationRepository(session).get(original.application.id).state
                is ApplicationState.APPLYING
            )
    finally:
        database.close()


@pytest.mark.parametrize("status", [HhApplyStatus.APPLIED, HhApplyStatus.AUTH_REQUIRED])
def test_another_account_task_is_unchanged_by_a_crossed_result(
    settings: Settings, status: HhApplyStatus
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            first = create_claim(session, "first")
            second = create_claim(session, "second")
        forged = replace(first, task=replace(second.task, application_id=first.application.id))
        with pytest.raises(ValueError), database.sessions.begin() as session:
            ApplicationAutomationService(session).record_result(
                forged, HhApplyResult(status, first.vacancy.source_url)
            )
        with database.sessions() as session:
            assert SystemStateRepository(session).get().state is SystemState.RUNNING
            for job in (first, second):
                assert QueueTaskRepository(session).get(job.task.id).state is TaskState.RUNNING
                assert (
                    ApplicationRepository(session).get(job.application.id).state
                    is ApplicationState.APPLYING
                )
    finally:
        database.close()


@pytest.mark.parametrize("observed_state", [ApplicationState.INVITED, ApplicationState.REJECTED])
def test_late_confirmation_preserves_a_more_recent_employer_outcome(
    settings: Settings, observed_state: ApplicationState
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            first = create_claim(session)
            second = next_attempt(session, first)
            application = session.get(ApplicationModel, first.application.id)
            assert application is not None
            application.state = observed_state
        for _ in range(2):
            with database.sessions.begin() as session:
                ApplicationAutomationService(session).record_result(
                    first, HhApplyResult(HhApplyStatus.APPLIED, first.vacancy.source_url)
                )
        with database.sessions() as session:
            assert ApplicationRepository(session).get(first.application.id).state is observed_state
            assert (
                QueueTaskRepository(session).get(second.task.id).state is TaskState.UNKNOWN_RESULT
            )
            confirmations = [
                event
                for event in ApplicationRepository(session).list_events(first.application.id)
                if event.event_type is ApplicationEventType.APPLIED
            ]
            assert len(confirmations) == 1 and confirmations[0].payload["attempt_number"] == 1
    finally:
        database.close()


def test_late_first_confirmation_does_not_reopen_a_completed_second_attempt(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            first = create_claim(session)
            second = next_attempt(session, first)
            ApplicationAutomationService(session).record_result(
                second, HhApplyResult(HhApplyStatus.APPLIED, second.vacancy.source_url)
            )
        with database.sessions.begin() as session:
            ApplicationAutomationService(session).record_result(
                first, HhApplyResult(HhApplyStatus.APPLIED, first.vacancy.source_url)
            )
        with database.sessions() as session:
            current = QueueTaskRepository(session).get(second.task.id)
            assert current.state is TaskState.COMPLETED and current.attempts == 2
            confirmations = [
                event
                for event in ApplicationRepository(session).list_events(first.application.id)
                if event.event_type is ApplicationEventType.APPLIED
            ]
            assert {event.payload["attempt_number"] for event in confirmations} == {1, 2}
    finally:
        database.close()
