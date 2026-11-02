from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from hugin.core.settings import Settings, get_settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationCandidatePoolModel,
    ApplicationSettingsModel,
    ApplicationTaskModel,
    CareerDirectionModel,
    CoverLetterModel,
    DirectionVacancyModel,
    VacancyModel,
)
from hugin.domain.automation import AutomationJobKind
from hugin.domain.content import CoverLetterState, cover_letter_instruction_version
from hugin.domain.directions import VacancyState
from hugin.domain.tasks import TaskState
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.tasks import QueueTaskRepository
from hugin.services.ai_prompts import AiPromptSettingsService
from hugin.services.application_automation import ApplicationAutomationService, ApplyJob
from hugin.services.application_candidate_pool import ApplicationCandidatePool
from hugin.services.application_selection_gate import ApplicationSelectionGate
from hugin.services.automation import AutomationSchedulerService
from hugin.services.queue import QueueService
from hugin.services.vacancy_analysis import RULES_VERSION
from tests.unit.test_application_automation_boundaries import queued_jobs


@pytest.fixture(autouse=True)
def enable_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUGIN_APPLICATION_POOL_TARGET", "40")
    monkeypatch.setenv("HUGIN_APPLICATION_POOL_MINIMUM", "30")
    get_settings.cache_clear()


def pool_jobs(
    session: Session, count: int, *, fresh_complete: bool = False
) -> tuple[ApplyJob, ...]:
    jobs = queued_jobs(session, count=count)
    account_id = jobs[0].application.account_id
    direction_id = jobs[0].application.direction_id
    assert direction_id is not None
    query = DirectionRepository(session).add_query(direction_id, "Python")
    options = session.get(ApplicationSettingsModel, 1)
    assert options is not None
    options.search_enabled = True
    scheduler = AutomationSchedulerService(session)
    now = datetime.now(UTC)
    scheduler.ensure_search_job(
        account_id=account_id, search_query_id=query.id, interval_minutes=120, now=now
    )
    if fresh_complete:
        job = scheduler.claim_due(now, allowed_kinds=(AutomationJobKind.SEARCH,))
        assert job is not None
        scheduler.complete(job.key, {"fresh_search_at": now.isoformat()}, now)
    session.flush()
    return jobs


@pytest.mark.parametrize(
    ("count", "fresh_complete", "allowed"),
    [
        (29, False, False),
        (30, False, False),
        (39, False, False),
        (40, False, True),
        (29, True, False),
        (30, True, True),
        (39, True, True),
    ],
)
def test_pool_thresholds(
    settings: Settings, count: int, fresh_complete: bool, allowed: bool
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, count, fresh_complete=fresh_complete)
            account_id = jobs[0].application.account_id
            reason = ApplicationSelectionGate(session).blocking_reason(account_id, progressive=True)
            assert (reason is None) is allowed
            assert session.scalar(select(ApplicationCandidatePoolModel)) is None
            if not allowed:
                assert str(count) in (reason or "")
            job = ApplicationAutomationService(session).claim_next(account_id=account_id)
            assert (job is not None) is allowed
            assert (session.scalar(select(ApplicationCandidatePoolModel)) is not None) is allowed
    finally:
        database.close()


def test_previous_day_pool_survives_consumption_and_restart_but_not_next_day(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 40)
            now = datetime.now(UTC)
            account_id = jobs[0].application.account_id
            for job in jobs:
                vacancy = session.get(VacancyModel, job.vacancy.id)
                assert vacancy is not None
                vacancy.created_at = now - timedelta(days=1)
                vacancy.published_at = now - timedelta(days=1)
            session.flush()
            first = ApplicationAutomationService(session).claim_next(account_id=account_id, now=now)
            assert first is not None
            for job in jobs[:15]:
                task = session.get(ApplicationTaskModel, job.task.id)
                assert task is not None
                task.state = TaskState.SKIPPED
        with database.sessions.begin() as session:
            assert (
                ApplicationSelectionGate(session).blocking_reason(account_id, now, progressive=True)
                is None
            )
            assert (
                ApplicationAutomationService(session).claim_next(account_id=account_id, now=now)
                is not None
            )
            assert (
                ApplicationSelectionGate(session).blocking_reason(
                    account_id, now + timedelta(days=1), progressive=True
                )
                is not None
            )
            direction = session.get(CareerDirectionModel, jobs[0].application.direction_id)
            assert direction is not None
            direction.scoring_config = {"changed": True}
            session.flush()
            assert (
                ApplicationSelectionGate(session).blocking_reason(account_id, now, progressive=True)
                is not None
            )
    finally:
        database.close()


def test_duplicate_direction_and_review_tasks_do_not_fill_pool(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 40)
            directions = DirectionRepository(session)
            other = directions.create(jobs[0].application.account_id, "Other")
            for job in jobs:
                directions.track_vacancy(other.id, job.vacancy.id)
                directions.apply_rules(
                    other.id,
                    job.vacancy.id,
                    state=VacancyState.QUEUED,
                    score=95,
                    details={"category": "MATCH"},
                    rules_version=RULES_VERSION,
                )
            row = session.get(ApplicationTaskModel, jobs[-1].task.id)
            assert row is not None
            row.state = TaskState.UNKNOWN_RESULT
            session.flush()
            assert (
                ApplicationAutomationService(session).claim_next(
                    account_id=jobs[0].application.account_id
                )
                is None
            )
    finally:
        database.close()


def test_ready_letter_does_not_skip_higher_ranked_pool_candidate(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 40)
            lower = jobs[1]
            session.add(
                CoverLetterModel(
                    application_id=lower.application.id,
                    vacancy_id=lower.vacancy.id,
                    direction_id=lower.application.direction_id,
                    resume_id=lower.resume.id,
                    instruction_version="pool-test",
                    model_name="test",
                    state=CoverLetterState.READY,
                    text="Prepared letter",
                )
            )
            session.flush()
            assert (
                QueueService(session).claim_next(
                    account_id=lower.application.account_id,
                    require_ready_cover_letter=True,
                    cover_letter_instruction_version="pool-test",
                )
                is None
            )
            assert all(t.attempts == 0 for t in session.scalars(select(ApplicationTaskModel)))
            preflight = ApplicationAutomationService(session).claim_next_form_preflight(
                account_id=lower.application.account_id
            )
            assert preflight is not None and preflight.task.id == jobs[0].task.id
    finally:
        database.close()


def test_pausing_search_does_not_complete_the_fresh_pass(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 30)
            options = session.get(ApplicationSettingsModel, 1)
            assert options is not None
            options.search_enabled = False
            session.flush()
            assert (
                ApplicationSelectionGate(session).blocking_reason(
                    jobs[0].application.account_id, progressive=True
                )
                is not None
            )
    finally:
        database.close()


def test_exact_prepared_claim_obeys_pool_threshold_and_global_ranking(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 40)
            lower = jobs[1]
            version = cover_letter_instruction_version(
                AiPromptSettingsService(session).get().cover_letter
            )
            session.add(
                CoverLetterModel(
                    application_id=lower.application.id,
                    vacancy_id=lower.vacancy.id,
                    direction_id=lower.application.direction_id,
                    resume_id=lower.resume.id,
                    instruction_version=version,
                    model_name="test",
                    state=CoverLetterState.READY,
                    text="Prepared letter",
                )
            )
            session.flush()
            service = ApplicationAutomationService(session)
            assert (
                service.claim_exact_prepared(
                    account_id=lower.application.account_id, task_id=lower.task.id
                )
                is None
            )
            assert QueueTaskRepository(session).get(lower.task.id).attempts == 0
            # A new day with fewer than 30 must block even this exact, prepared candidate.
            for job in jobs[2:]:
                row = session.get(ApplicationTaskModel, job.task.id)
                assert row is not None
                row.state = TaskState.SKIPPED
            session.flush()
            assert (
                service.claim_exact_prepared(
                    account_id=lower.application.account_id,
                    task_id=lower.task.id,
                    now=datetime.now(UTC) + timedelta(days=1),
                )
                is None
            )
    finally:
        database.close()


def test_fresh_fallback_waits_for_the_current_day_evaluations(settings: Settings) -> None:
    from hugin.domain.vacancies import VacancyData
    from hugin.repositories.vacancies import VacancyRepository

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 30, fresh_complete=True)
            vacancy = VacancyRepository(session).upsert(
                VacancyData("pending-current", "Python", "https://hh.ru/vacancy/pending-current")
            )
            direction_id = jobs[0].application.direction_id
            assert direction_id is not None
            DirectionRepository(session).track_vacancy(direction_id, vacancy.id)
            assert (
                ApplicationSelectionGate(session).blocking_reason(
                    jobs[0].application.account_id, progressive=True
                )
                is not None
            )
    finally:
        database.close()


def test_form_preflight_cannot_bypass_unopened_pool(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 29, fresh_complete=True)
            assert (
                ApplicationAutomationService(session).claim_next_form_preflight(
                    account_id=jobs[0].application.account_id
                )
                is None
            )
            assert QueueTaskRepository(session).get(jobs[0].task.id).attempts == 0
    finally:
        database.close()


def test_newly_evaluated_candidate_joins_open_pool_without_accumulating_again(
    settings: Settings,
) -> None:
    from hugin.domain.vacancies import VacancyData
    from hugin.repositories.vacancies import VacancyRepository

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 40)
            account_id = jobs[0].application.account_id
            direction_id = jobs[0].application.direction_id
            assert direction_id is not None
            assert (
                ApplicationAutomationService(session).claim_next(account_id=account_id) is not None
            )
            opened = session.scalar(select(ApplicationCandidatePoolModel))
            assert opened is not None
            opened_at = opened.opened_at
            for task in session.scalars(select(ApplicationTaskModel)):
                task.state = TaskState.SKIPPED
            late = VacancyRepository(session).upsert(
                VacancyData(
                    "new-after-opening",
                    "Python developer",
                    "https://hh.ru/vacancy/new-after-opening",
                )
            )
            DirectionRepository(session).track_vacancy(direction_id, late.id)
        with database.sessions.begin() as session:
            service = ApplicationAutomationService(session)
            assert service.claim_next(account_id=account_id) is None
            old_pending = session.get(ApplicationTaskModel, jobs[-1].task.id)
            assert old_pending is not None
            old_pending.state = TaskState.PENDING
            vacancy = session.get(VacancyModel, late.id)
            assert vacancy is not None
            vacancy.details_fetched_at = datetime.now(UTC)
            session.flush()
            DirectionRepository(session).apply_rules(
                direction_id,
                late.id,
                state=VacancyState.ANALYZED,
                score=99,
                details={"category": "MATCH", "fit_tier": 1},
                rules_version=RULES_VERSION,
            )
            chosen = service.claim_next(account_id=account_id)
            assert chosen is not None and chosen.vacancy.id == late.id
            saved = session.scalar(select(ApplicationCandidatePoolModel))
            assert saved is not None and saved.opened_at == opened_at
            assert late.id in saved.vacancy_ids and len(saved.vacancy_ids) == 41
    finally:
        database.close()


def test_pool_does_not_transfer_rejected_descriptions(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 2)
            account_id = jobs[0].application.account_id
            accepted_id = jobs[0].vacancy.id
            rejected = jobs[1]
            vacancy = session.get(VacancyModel, rejected.vacancy.id)
            tracked = session.get(
                DirectionVacancyModel, (rejected.application.direction_id, rejected.vacancy.id)
            )
            assert vacancy is not None and tracked is not None
            vacancy.description = "Rejected vacancy historical text " * 100_000
            tracked.state = VacancyState.FILTERED_OUT
            tracked.rules_details = {"category": "REJECT", "reason": "Different profession"}

        transferred = 0

        def capture(_connection: Any, cursor: Any, *_args: Any) -> None:
            nonlocal transferred
            result = cursor.pgresult
            if result is not None:
                transferred += sum(
                    len(result.get_value(row, column) or b"")
                    for row in range(result.ntuples)
                    for column in range(result.nfields)
                )

        event.listen(database.engine, "after_cursor_execute", capture)
        with database.sessions() as session:
            status = ApplicationCandidatePool(session).status(account_id)
            assert status.eligible_ids == frozenset({accepted_id})
        assert transferred < 100_000, f"Pool read transferred {transferred} bytes"
    finally:
        database.close()


def test_pool_retires_rejected_tasks_even_before_threshold_without_changing_unknown(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = pool_jobs(session, 3)
            for job in jobs[:2]:
                tracked = session.get(
                    DirectionVacancyModel, (job.application.direction_id, job.vacancy.id)
                )
                assert tracked is not None
                tracked.state = VacancyState.FILTERED_OUT
                tracked.rules_details = {"category": "REJECT"}
            unknown = session.get(ApplicationTaskModel, jobs[1].task.id)
            assert unknown is not None
            unknown.state = TaskState.UNKNOWN_RESULT
            unknown.last_error_code = "UNKNOWN_RESULT"
            session.flush()

            assert (
                ApplicationCandidatePool(session).admit(
                    jobs[0].application.account_id, datetime.now(UTC)
                )
                == frozenset()
            )
            assert QueueTaskRepository(session).get(jobs[0].task.id).state is TaskState.SKIPPED
            assert (
                QueueTaskRepository(session).get(jobs[0].task.id).last_error_code
                == "VACANCY_RULES_CHANGED"
            )
            assert (
                QueueTaskRepository(session).get(jobs[1].task.id).state is TaskState.UNKNOWN_RESULT
            )
            assert QueueTaskRepository(session).get(jobs[2].task.id).state is TaskState.PENDING
    finally:
        database.close()
