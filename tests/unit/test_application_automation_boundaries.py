from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
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
from hugin.domain.applications import EventPayload
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


def test_unchanged_republication_is_stopped_before_queue_and_before_attempt(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            previous, current = queued_jobs(session)
            vacancies = VacancyRepository(session)
            for job in (previous, current):
                vacancies.upsert(
                    VacancyData(
                        job.vacancy.hh_id,
                        "Python developer",
                        job.vacancy.source_url,
                        employer_name="Agency",
                        description="Python for client A.",
                        region="Kazan",
                        details_fetched_at=datetime.now(UTC),
                    )
                )
            ApplicationRepository(session).transition_state(
                previous.application.id,
                ApplicationState.APPLIED,
                {"source": "hugin_send", "hh_status": "APPLIED"},
            )
            service = ApplicationAutomationService(session)
            monkeypatch.setattr(service, "_semantic_selection_current", lambda *_args: True)
            account = AccountRepository(session).get(previous.application.account_id)
            direction = DirectionRepository(session).get_for_account(
                account.id,
                current.application.direction_id or 0,
            )
            assert service._prepare_tracked(
                account, direction, current.resume, current.direction_vacancy
            ) == (0, 1)
            task = QueueTaskRepository(session).get(current.task.id)
            assert task.state is TaskState.REVIEW_REQUIRED
            assert task.last_error_code == "REPOST_INTERVAL"
            # A stale queue entry must face the same check after it was already claimed.
            QueueTaskRepository(session).transition(
                task.id, TaskState.RETRY_SCHEDULED, scheduled_at=datetime.now(UTC)
            )
            QueueTaskRepository(session).transition(task.id, TaskState.RUNNING)
            monkeypatch.setattr(service, "_application_selection_current", lambda *_args: True)
            job = replace(current, vacancy=vacancies.get(current.vacancy.id))
            assert service._save_attempt(job) is None
            assert QueueTaskRepository(session).get(task.id).last_error_code == "REPOST_INTERVAL"
            assert not service._repost_submission_allowed(current.application, datetime.now(UTC))
            # The same employer's other client remains eligible.
            changed = vacancies.upsert(
                replace(
                    VacancyData(
                        job.vacancy.hh_id,
                        "Python developer",
                        job.vacancy.source_url,
                        employer_name="Agency",
                        description="Python for client B.",
                        region="Kazan",
                        details_fetched_at=datetime.now(UTC),
                    )
                )
            )
            from hugin.services.reapplication_policy import ReapplicationPolicyService

            assert (
                ReapplicationPolicyService(session)
                .evaluate(account.id, changed, exclude_application_id=current.application.id)
                .allowed
            )
            for job, language in ((previous, "Python"), (current, "Java")):
                vacancies.upsert(
                    VacancyData(
                        job.vacancy.hh_id,
                        "Developer",
                        job.vacancy.source_url,
                        employer_name="Agency",
                        description="Internal services for the company.",
                        required_qualifications=language,
                        region="Kazan",
                        details_fetched_at=datetime.now(UTC),
                    )
                )
            assert (
                ReapplicationPolicyService(session)
                .evaluate(
                    account.id,
                    vacancies.get(current.vacancy.id),
                    exclude_application_id=current.application.id,
                )
                .allowed
            )
    finally:
        database.close()


def test_repost_history_keeps_rejection_and_does_not_use_import_time_or_fact_ids(
    settings: Settings,
) -> None:
    from hugin.database.models import ApplicationEventModel, ApplicationStatusObservationModel
    from hugin.services.reapplication_policy import ReapplicationPolicyService

    database = create_database(settings)
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            previous, current = queued_jobs(session)
            vacancies = VacancyRepository(session)
            for job in (previous, current):
                vacancies.upsert(
                    VacancyData(
                        job.vacancy.hh_id,
                        "Python developer",
                        job.vacancy.source_url,
                        employer_name="Agency",
                        description="Python for client A.",
                        details_fetched_at=now,
                    )
                )
            session.add(
                ApplicationEventModel(
                    application_id=previous.application.id,
                    event_type=ApplicationEventType.APPLIED,
                    created_at=now - timedelta(days=40),
                    payload={
                        "source": "hugin_send",
                        "hh_status": "APPLIED",
                        "outcome_context": {
                            "profile": {
                                "profile_facts": [
                                    {
                                        "id": 1,
                                        "category": "experience",
                                        "content": "Python services",
                                    }
                                ]
                            }
                        },
                    },
                )
            )
            session.flush()
            service = ReapplicationPolicyService(session)
            vacancy = vacancies.get(current.vacancy.id)
            facts: list[dict[str, object]] = [
                {"id": 2, "category": "experience", "content": "Python services"}
            ]
            profile: EventPayload = {"profile_facts": facts}
            assert service.evaluate(
                previous.application.account_id,
                vacancy,
                exclude_application_id=current.application.id,
                profile=profile,
            ).allowed
            facts.append({"category": "experience", "content": "New commercial experience"})
            assert (
                service.evaluate(
                    previous.application.account_id,
                    vacancy,
                    exclude_application_id=current.application.id,
                    profile=profile,
                ).code
                == "REPOST_PROFILE_CHANGED"
            )
            session.add(
                ApplicationStatusObservationModel(
                    application_id=previous.application.id,
                    state=ApplicationState.REJECTED,
                    checked_at=now - timedelta(days=39),
                    recorded_at=now,
                )
            )
            session.flush()
            assert (
                service.evaluate(
                    previous.application.account_id,
                    vacancy,
                    exclude_application_id=current.application.id,
                ).code
                == "REPOST_REJECTION"
            )
            session.add(
                ApplicationEventModel(
                    application_id=previous.application.id,
                    event_type=ApplicationEventType.APPLIED,
                    created_at=now - timedelta(days=60),
                    payload={"source": "hh.ru", "hh_status": "APPLIED"},
                )
            )
            session.flush()
            assert (
                service.evaluate(
                    previous.application.account_id,
                    vacancy,
                    exclude_application_id=current.application.id,
                ).code
                == "REPOST_HISTORY_UNKNOWN"
            )
    finally:
        database.close()


def test_changed_role_review_is_scoped_and_expires_when_sources_or_conversation_change(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hugin.database.models import ApplicationEventModel
    from hugin.services.reapplication_policy import ReapplicationPolicyService

    database = create_database(settings)
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            previous, current, other = queued_jobs(session, count=3)
            vacancies = VacancyRepository(session)
            for job in (previous, current, other):
                vacancies.upsert(
                    VacancyData(
                        job.vacancy.hh_id,
                        "Python developer",
                        job.vacancy.source_url,
                        employer_name="Employer",
                        description="Internal Python API."
                        if job is previous
                        else "Internal Python API, new requirements.",
                        details_fetched_at=now,
                    )
                )
            for job in (current, other):
                vacancies.mark_duplicate(job.vacancy.id, previous.vacancy.id, 0.95)
            ApplicationRepository(session).transition_state(
                previous.application.id,
                ApplicationState.APPLIED,
                {"source": "hugin_send", "hh_status": "APPLIED"},
            )
            now = datetime.now(UTC)
            confirmation = session.scalar(
                select(ApplicationEventModel).where(
                    ApplicationEventModel.application_id == previous.application.id,
                    ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                )
            )
            assert confirmation is not None
            confirmation.created_at = now - timedelta(days=31)
            monkeypatch.setattr(
                ApplicationAutomationService, "_semantic_selection_current", lambda *_args: True
            )
            monkeypatch.setattr(
                ApplicationAutomationService, "_application_selection_current", lambda *_args: True
            )
            automation = ApplicationAutomationService(session)
            account = AccountRepository(session).get(current.application.account_id)
            direction = DirectionRepository(session).get_for_account(
                account.id, current.application.direction_id or 0
            )
            assert automation._prepare_tracked(
                account, direction, current.resume, current.direction_vacancy
            ) == (0, 1)
            policy = ReapplicationPolicyService(session)
            with pytest.raises(LookupError):
                policy.review_changed_application(
                    account.id + 99999,
                    current.application.id,
                    reason="Проверены изменённые требования и подтверждённое соответствие.",
                )
            with pytest.raises(ValueError):
                policy.review_changed_application(account.id, current.application.id, reason="да")
            preview = policy.evaluate(
                account.id,
                vacancies.get(current.vacancy.id),
                exclude_application_id=current.application.id,
                use_review=False,
            )
            assert preview.review_key
            with pytest.raises(ValueError, match="изменились"):
                policy.review_changed_application(
                    account.id,
                    current.application.id,
                    reason=(
                        "Новые требования проверены по полному тексту и подтверждённому профилю."
                    ),
                    expected_review_key="0" * 64,
                )
            decision = policy.review_changed_application(
                account.id,
                current.application.id,
                reason="Новые требования проверены по полному тексту и подтверждённому профилю.",
                expected_review_key=preview.review_key,
            )
            assert decision.allowed
            assert automation._prepare_tracked(
                account, direction, current.resume, current.direction_vacancy
            ) == (1, 0)
            now = datetime.now(UTC)
            assert automation._repost_submission_allowed(current.application, now)
            assert (
                policy.evaluate(
                    account.id,
                    vacancies.get(other.vacancy.id),
                    exclude_application_id=other.application.id,
                ).code
                == "REPOST_CHANGED"
            )
            events = list(
                session.scalars(
                    select(ApplicationEventModel).where(
                        ApplicationEventModel.application_id == current.application.id,
                        ApplicationEventModel.event_type == ApplicationEventType.APPLY_INTENT,
                    )
                )
            )
            assert any(event.payload.get("source") == "reapplication_review" for event in events)
            assert (
                policy.evaluate(
                    account.id,
                    vacancies.get(current.vacancy.id),
                    exclude_application_id=current.application.id,
                    now=now + timedelta(days=8),
                ).code
                == "REPOST_CHANGED"
            )
            vacancies.upsert(
                VacancyData(
                    current.vacancy.hh_id,
                    "Python developer",
                    current.vacancy.source_url,
                    employer_name="Employer",
                    description="Internal Python API, another change.",
                    details_fetched_at=now,
                )
            )
            assert not automation._repost_submission_allowed(current.application, now)
            vacancies.upsert(
                VacancyData(
                    current.vacancy.hh_id,
                    "Python developer",
                    current.vacancy.source_url,
                    employer_name="Employer",
                    description="Internal Python API.",
                    details_fetched_at=now,
                )
            )
            assert automation._prepare_tracked(
                account, direction, current.resume, current.direction_vacancy
            ) == (0, 1)
            assert policy.evaluate(
                account.id,
                vacancies.get(current.vacancy.id),
                exclude_application_id=current.application.id,
            ).allowed
            with pytest.raises(ValueError):
                policy.review_changed_application(
                    account.id,
                    current.application.id,
                    reason="Номер изменён, но сама роль осталась совершенно прежней.",
                )
            vacancies.upsert(
                VacancyData(
                    current.vacancy.hh_id,
                    "Python developer",
                    current.vacancy.source_url,
                    employer_name="Employer",
                    description="Internal Python API, new requirements.",
                    details_fetched_at=now,
                )
            )
            ApplicationRepository(session).transition_state(
                previous.application.id, ApplicationState.INVITED
            )
            assert (
                policy.evaluate(
                    account.id,
                    vacancies.get(current.vacancy.id),
                    exclude_application_id=current.application.id,
                ).code
                == "REPOST_CONVERSATION"
            )
    finally:
        database.close()


def test_confirmed_imported_work_experience_can_be_reviewed_after_rejection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hugin.database.models import CandidateProfileModel, VerifiedFactModel
    from hugin.domain.content import ConfirmationState
    from hugin.services.reapplication_policy import ReapplicationPolicyService

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            previous, current = queued_jobs(session)
            vacancies = VacancyRepository(session)
            for job in (previous, current):
                vacancies.upsert(
                    VacancyData(
                        job.vacancy.hh_id,
                        "Python developer",
                        job.vacancy.source_url,
                        employer_name="Employer",
                        description="Internal Python API.",
                        details_fetched_at=datetime.now(UTC),
                    )
                )
            apps = ApplicationRepository(session)
            apps.transition_state(
                previous.application.id,
                ApplicationState.APPLIED,
                {
                    "source": "hugin_send",
                    "hh_status": "APPLIED",
                    "selection_snapshot": ApplicationAutomationService._selection_snapshot(
                        replace(previous, profile_snapshot={"profile_facts": []})
                    ),
                },
            )
            apps.transition_state(previous.application.id, ApplicationState.REJECTED)
            from hugin.database.models import ApplicationEventModel

            confirmation = session.scalar(
                select(ApplicationEventModel).where(
                    ApplicationEventModel.application_id == previous.application.id,
                    ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                )
            )
            assert confirmation is not None
            confirmation.created_at = datetime.now(UTC) - timedelta(days=31)
            monkeypatch.setattr(
                ApplicationAutomationService, "_semantic_selection_current", lambda *_args: True
            )
            monkeypatch.setattr(
                ApplicationAutomationService, "_application_selection_current", lambda *_args: True
            )
            automation = ApplicationAutomationService(session)
            account = AccountRepository(session).get(current.application.account_id)
            direction = DirectionRepository(session).get_for_account(
                account.id, current.application.direction_id or 0
            )
            assert automation._prepare_tracked(
                account, direction, current.resume, current.direction_vacancy
            ) == (0, 1)
            policy = ReapplicationPolicyService(session)
            with pytest.raises(ValueError):
                policy.review_changed_application(
                    account.id,
                    current.application.id,
                    reason="Прежний отказ есть, но существенного изменения опыта пока нет.",
                )
            profile = CandidateProfileModel(account_id=account.id, display_name="Candidate")
            session.add(profile)
            session.flush()
            fact = VerifiedFactModel(
                profile_id=profile.id,
                category="work_experience",
                content="New confirmed Python employment",
                source_type="user",
                state=ConfirmationState.CONFIRMED,
                allow_in_letters=True,
            )
            session.add(fact)
            session.flush()
            assert (
                policy.evaluate(
                    account.id,
                    vacancies.get(current.vacancy.id),
                    exclude_application_id=current.application.id,
                ).code
                == "REPOST_PROFILE_CHANGED"
            )
            monkeypatch.setattr(
                ApplicationAutomationService, "_application_selection_current", lambda *_args: False
            )
            with pytest.raises(ValueError, match="отбор"):
                policy.review_changed_application(
                    account.id,
                    current.application.id,
                    reason="Новый подтверждённый опыт снимает прежнее препятствие для этой роли.",
                )
            monkeypatch.setattr(
                ApplicationAutomationService, "_application_selection_current", lambda *_args: True
            )
            assert policy.review_changed_application(
                account.id,
                current.application.id,
                reason="Новый подтверждённый опыт снимает прежнее препятствие для этой роли.",
            ).allowed
            fact.content = "Another confirmed change of experience"
            session.flush()
            assert not automation._repost_submission_allowed(current.application, datetime.now(UTC))
    finally:
        database.close()


@pytest.mark.parametrize("ages, expected", [((0,), "REPOST_INTERVAL"), ((80, 40), "REPOST_LIMIT")])
def test_reviewed_changed_role_keeps_interval_and_rolling_limit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, ages: tuple[int, ...], expected: str
) -> None:
    from hugin.database.models import ApplicationEventModel
    from hugin.services.reapplication_policy import ReapplicationPolicyService

    database = create_database(settings)
    monkeypatch.setattr(
        ApplicationAutomationService, "_semantic_selection_current", lambda *_: True
    )
    monkeypatch.setattr(
        ApplicationAutomationService, "_application_selection_current", lambda *_: True
    )
    try:
        with database.sessions.begin() as session:
            jobs = queued_jobs(session, count=len(ages) + 1)
            current = jobs[-1]
            vacancies = VacancyRepository(session)
            for job in jobs:
                vacancies.upsert(
                    VacancyData(
                        job.vacancy.hh_id,
                        "Python developer",
                        job.vacancy.source_url,
                        employer_name="Employer",
                        description="Python API, changed requirements."
                        if job is current
                        else "Python API.",
                        details_fetched_at=datetime.now(UTC),
                    )
                )
            for job in jobs[1:]:
                vacancies.mark_duplicate(job.vacancy.id, jobs[0].vacancy.id, 0.95)
            for job, days in zip(jobs[:-1], ages, strict=True):
                ApplicationRepository(session).transition_state(
                    job.application.id,
                    ApplicationState.APPLIED,
                    {"source": "hugin_send", "hh_status": "APPLIED"},
                )
                event = session.scalar(
                    select(ApplicationEventModel).where(
                        ApplicationEventModel.application_id == job.application.id,
                        ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                    )
                )
                assert event is not None
                event.created_at = datetime.now(UTC) - timedelta(days=days)
            automation = ApplicationAutomationService(session)
            account = AccountRepository(session).get(current.application.account_id)
            direction = DirectionRepository(session).get_for_account(
                account.id, current.application.direction_id or 0
            )
            automation._prepare_tracked(
                account, direction, current.resume, current.direction_vacancy
            )
            decision = ReapplicationPolicyService(session).review_changed_application(
                account.id,
                current.application.id,
                reason="Подтверждённое изменение требований проверено по полному тексту роли.",
            )
            assert not decision.allowed and decision.code == expected
            assert decision.recheck_at is not None
            assert not automation._repost_submission_allowed(current.application, datetime.now(UTC))
    finally:
        database.close()


@pytest.mark.parametrize("code", ["CAPTCHA_REQUIRED", "HH_RATE_LIMITED"])
def test_unconfirmed_submission_blocks_next_attempt_when_history_is_blocked(
    settings: Settings, code: str
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            queued = queued_jobs(session)
            service = ApplicationAutomationService(session)
            account_id = queued[0].application.account_id
            job = service.claim_next(account_id=account_id)
            assert job is not None
            now = datetime.now(UTC)
            result = service.record_result(
                job,
                HhApplyResult(
                    HhApplyStatus.UNKNOWN_RESULT,
                    "https://hh.ru/applicant/negotiations",
                    reconciliation_error_code=code,
                    retry_after_seconds=120 if code == "HH_RATE_LIMITED" else None,
                ),
                now=now,
            )
            assert result.blocking and not result.sent
            assert QueueTaskRepository(session).get(job.task.id).state is TaskState.UNKNOWN_RESULT
            assert (
                ApplicationRepository(session).get(job.application.id).state
                is ApplicationState.APPLYING
            )
            if code == "CAPTCHA_REQUIRED":
                assert SystemStateRepository(session).get().state is SystemState.CAPTCHA_REQUIRED
            else:
                assert result.next_apply_at == now + timedelta(seconds=120)
            assert service.claim_next(account_id=account_id) is None
    finally:
        database.close()


@pytest.mark.parametrize("recent", [True, False])
def test_two_recent_unknown_results_pause_without_retrying_either_application(
    settings: Settings,
    recent: bool,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            queued = queued_jobs(session, count=3)
            service = ApplicationAutomationService(session)
            account_id = queued[0].application.account_id
            for index in range(2):
                job = service.claim_next(account_id=account_id)
                assert job is not None
                outcome = service.record_result(
                    job, HhApplyResult(HhApplyStatus.UNKNOWN_RESULT, job.vacancy.source_url)
                )
                assert outcome.blocking is (index == 1 and recent)
                assert (
                    QueueTaskRepository(session).get(job.task.id).state is TaskState.UNKNOWN_RESULT
                )
                if index == 0 and not recent:
                    stored_task = session.get(ApplicationTaskModel, job.task.id)
                    assert stored_task is not None
                    stored_task.updated_at = datetime.now(UTC) - timedelta(days=1)
                    session.flush()
            expected_state = SystemState.PAUSED if recent else SystemState.RUNNING
            assert SystemStateRepository(session).get().state is expected_state
            assert (service.claim_next(account_id=account_id) is None) is recent
    finally:
        database.close()


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
                details_fetched_at=datetime.now(UTC),
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
