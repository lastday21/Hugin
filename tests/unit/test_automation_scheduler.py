from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain import (
    AutomationJobKind,
    AutomationJobState,
    SystemState,
    TaskState,
    VacancyData,
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
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.automation import AutomationSchedulerService

pytestmark = pytest.mark.integration


def seed_search_query(settings: Settings) -> tuple[int, int]:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Фоновая проверка")
            direction = DirectionRepository(session).create(account.id, "Python backend")
            query = DirectionRepository(session).add_query(
                direction.id,
                "Python backend",
                schedule_minutes=120,
            )
            return account.id, query.id
    finally:
        database.close()


def test_scheduler_uses_resource_saving_intervals_and_does_not_catch_up(
    settings: Settings,
) -> None:
    account_id, query_id = seed_search_query(settings)
    database = create_database(settings)
    due_at = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            messages, statuses = scheduler.ensure_account_jobs(account_id, due_at)
            search = scheduler.ensure_search_job(
                account_id=account_id,
                search_query_id=query_id,
                interval_minutes=120,
                now=due_at,
            )

            assert messages.interval_seconds == 15 * 60
            assert statuses.interval_seconds == 60 * 60
            assert search.interval_seconds == 240 * 60
            assert messages.next_run_at == statuses.next_run_at == search.next_run_at == due_at

        messages_finished_at = due_at + timedelta(hours=2, seconds=10)
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            claimed = scheduler.claim_due(messages_finished_at)
            assert claimed is not None
            assert claimed.kind is AutomationJobKind.MESSAGES
            completed = scheduler.complete(
                claimed.key,
                {"new_messages": 2},
                messages_finished_at,
            )

            assert completed.state is AutomationJobState.WAITING
            assert completed.last_success_at == messages_finished_at
            assert completed.last_result == {"new_messages": 2}
            assert completed.next_run_at == messages_finished_at + timedelta(minutes=15)

        statuses_finished_at = messages_finished_at + timedelta(seconds=10)
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            claimed = scheduler.claim_due(statuses_finished_at)
            assert claimed is not None
            assert claimed.kind is AutomationJobKind.STATUSES
            completed = scheduler.complete(claimed.key, now=statuses_finished_at)
            assert completed.next_run_at == statuses_finished_at + timedelta(minutes=60)

        search_finished_at = statuses_finished_at + timedelta(seconds=10)
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            claimed = scheduler.claim_due(search_finished_at)
            assert claimed is not None
            assert claimed.kind is AutomationJobKind.SEARCH
            completed = scheduler.complete(claimed.key, now=search_finished_at)
            assert completed.next_run_at == search_finished_at + timedelta(minutes=240)
    finally:
        database.close()


def test_scheduler_limits_failure_backoff(settings: Settings) -> None:
    account_id, _query_id = seed_search_query(settings)
    database = create_database(settings)
    current = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    expected_delays = (1, 5, 15, 15)

    try:
        with database.sessions.begin() as session:
            AutomationSchedulerService(session).ensure_account_jobs(account_id, current)

        for failure_number, delay_minutes in enumerate(expected_delays, start=1):
            with database.sessions.begin() as session:
                scheduler = AutomationSchedulerService(session)
                claimed = scheduler.claim_due(current)
                assert claimed is not None
                assert claimed.kind is AutomationJobKind.MESSAGES
                failed = scheduler.fail(
                    claimed.key,
                    error_code="NETWORK_" + "X" * 80,
                    error_message="  Временная   ошибка сети  ",
                    now=current,
                )

                assert failed.state is AutomationJobState.FAILED
                assert failed.consecutive_failures == failure_number
                assert failed.next_run_at == current + timedelta(minutes=delay_minutes)
                assert len(failed.last_error_code or "") == 64
                assert failed.last_error_message == "Временная ошибка сети"

            current += timedelta(minutes=delay_minutes)
    finally:
        database.close()


def test_scheduler_defers_without_marking_job_failed(settings: Settings) -> None:
    account_id, _query_id = seed_search_query(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            scheduler.ensure_account_jobs(account_id, now)
            claimed = scheduler.claim_due(now)
            assert claimed is not None
            deferred = scheduler.defer(
                claimed.key,
                retry_after_seconds=60,
                result={"deferred": True, "reason": "APPLICATIONS_PENDING"},
                now=now,
            )

            assert deferred.state is AutomationJobState.WAITING
            assert deferred.next_run_at == now + timedelta(seconds=60)
            assert deferred.last_success_at is None
            assert deferred.consecutive_failures == 0
            assert deferred.last_error_code is None
            assert deferred.last_result == {
                "deferred": True,
                "reason": "APPLICATIONS_PENDING",
            }
    finally:
        database.close()


def test_scheduler_respects_explicit_retry_delay(settings: Settings) -> None:
    account_id, _query_id = seed_search_query(settings)
    database = create_database(settings)
    current = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            AutomationSchedulerService(session).ensure_account_jobs(account_id, current)
            claimed = AutomationSchedulerService(session).claim_due(current)
            assert claimed is not None
            failed = AutomationSchedulerService(session).fail(
                claimed.key,
                error_code="HH_RATE_LIMITED",
                error_message="hh.ru временно ограничил обращения",
                retry_after_seconds=180,
                now=current,
            )

        assert failed.next_run_at == current + timedelta(seconds=180)
    finally:
        database.close()


def test_scheduler_recovers_only_stale_running_jobs(settings: Settings) -> None:
    account_id, query_id = seed_search_query(settings)
    database = create_database(settings)
    started_at = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            scheduler.ensure_search_job(
                account_id=account_id,
                search_query_id=query_id,
                interval_minutes=120,
                now=started_at,
            )
            claimed = scheduler.claim_due(started_at)
            assert claimed is not None
            assert claimed.state is AutomationJobState.RUNNING

        heartbeat_at = started_at + timedelta(minutes=4)
        with database.sessions.begin() as session:
            AutomationSchedulerService(session).heartbeat(claimed.key, heartbeat_at)

        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            assert scheduler.recover_stale(started_at + timedelta(minutes=8)) == ()

        recovered_at = started_at + timedelta(minutes=10)
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            recovered = scheduler.recover_stale(recovered_at)
            assert len(recovered) == 1
            assert recovered[0].state is AutomationJobState.FAILED
            assert recovered[0].last_error_code == "AUTOMATION_INTERRUPTED"
            assert recovered[0].next_run_at == recovered_at + timedelta(minutes=1)

        with database.sessions.begin() as session:
            assert (
                AutomationSchedulerService(session).recover_stale(recovered_at + timedelta(hours=1))
                == ()
            )
    finally:
        database.close()


def test_due_job_is_claimed_by_only_one_transaction(settings: Settings) -> None:
    account_id, query_id = seed_search_query(settings)
    database = create_database(settings)
    due_at = datetime(2026, 7, 26, 11, 0, tzinfo=UTC)

    first_session = database.sessions()
    second_session = database.sessions()
    try:
        with first_session.begin():
            first_scheduler = AutomationSchedulerService(first_session)
            first_scheduler.ensure_search_job(
                account_id=account_id,
                search_query_id=query_id,
                interval_minutes=120,
                now=due_at,
            )

        with first_session.begin():
            first = AutomationSchedulerService(first_session).claim_due(due_at)
            assert first is not None
            assert first.state is AutomationJobState.RUNNING

            with second_session.begin():
                second = AutomationSchedulerService(second_session).claim_due(due_at)
                assert second is None
    finally:
        first_session.close()
        second_session.close()
        database.close()


def test_blocked_and_disabled_jobs_are_not_claimed(settings: Settings) -> None:
    account_id, _query_id = seed_search_query(settings)
    database = create_database(settings)
    due_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            messages, statuses = scheduler.ensure_account_jobs(account_id, due_at)
            blocked = scheduler.block(
                messages.key,
                error_code="AUTH_REQUIRED",
                error_message="Требуется вход",
                now=due_at,
            )
            disabled = scheduler.disable(statuses.key, due_at)

            assert blocked.state is AutomationJobState.BLOCKED
            assert blocked.next_run_at is None
            assert disabled.state is AutomationJobState.DISABLED
            assert disabled.next_run_at is None
            assert scheduler.claim_due(due_at + timedelta(days=1)) is None

        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            SystemStateRepository(session).transition(SystemState.PAUSED)
            enabled = scheduler.enable(statuses.key, due_at + timedelta(days=1))
            unblocked = scheduler.unblock(messages.key, due_at + timedelta(days=1))
            assert enabled.state is AutomationJobState.WAITING
            assert unblocked.state is AutomationJobState.WAITING
            claimed = scheduler.claim_due(due_at + timedelta(days=1))
            assert claimed is not None
            assert claimed.kind is AutomationJobKind.MESSAGES
    finally:
        database.close()


@pytest.mark.parametrize(
    ("error_code", "protective_state"),
    (
        ("INVALID_CREDENTIALS", SystemState.AUTH_REQUIRED),
        ("CAPTCHA_REQUIRED", SystemState.CAPTCHA_REQUIRED),
        ("ACCOUNT_WARNING", SystemState.ACCOUNT_WARNING),
    ),
)
def test_protective_system_state_stops_all_background_jobs(
    settings: Settings,
    error_code: str,
    protective_state: SystemState,
) -> None:
    account_id, _query_id = seed_search_query(settings)
    database = create_database(settings)
    due_at = datetime(2026, 7, 26, 12, 30, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            messages, statuses = scheduler.ensure_account_jobs(account_id, due_at)
            scheduler.block(
                messages.key,
                error_code=error_code,
                error_message="Требуется действие пользователя",
                now=due_at,
            )

            assert SystemStateRepository(session).get().state is protective_state
            assert scheduler.claim_due(due_at) is None
            jobs = {job.key: job for job in scheduler.list_for_account(account_id)}
            assert jobs[messages.key].state is AutomationJobState.BLOCKED
            assert jobs[statuses.key].state is AutomationJobState.WAITING
    finally:
        database.close()


def test_authentication_restores_the_previous_queue_mode(settings: Settings) -> None:
    account_id, _query_id = seed_search_query(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 26, 12, 45, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            messages, _statuses = scheduler.ensure_account_jobs(account_id, now)
            state = SystemStateRepository(session)
            state.transition(SystemState.RUNNING)
            scheduler.block(
                messages.key,
                error_code="CAPTCHA_REQUIRED",
                error_message="Требуется проверка",
                now=now,
            )

            assert state.get().state is SystemState.CAPTCHA_REQUIRED
            ApplicationAutomationService(session).resume_after_authentication()
            assert state.get().state is SystemState.RUNNING

            state.transition(SystemState.PAUSED)
            state.transition(SystemState.AUTH_REQUIRED)
            ApplicationAutomationService(session).resume_after_authentication()
            assert state.get().state is SystemState.PAUSED
    finally:
        database.close()


def test_unknown_result_accelerates_status_reconciliation(settings: Settings) -> None:
    account_id, _query_id = seed_search_query(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 26, 12, 50, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            messages, statuses = scheduler.ensure_account_jobs(account_id, now)
            scheduler.disable(messages.key, now)

            resume = ResumeRepository(session).upsert(
                account_id,
                "unknown-reconciliation-resume",
                "Python backend",
            )
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "unknown-reconciliation-vacancy",
                    "Python backend",
                    "https://hh.ru/vacancy/unknown-reconciliation-vacancy",
                )
            )
            application = ApplicationRepository(session).create_apply_intent(
                account_id,
                vacancy.id,
                resume.id,
            )
            tasks = QueueTaskRepository(session)
            task = tasks.enqueue(application.id, 50, now)
            assert tasks.claim_exact(task.id, now) is not None
            tasks.transition(
                task.id,
                TaskState.UNKNOWN_RESULT,
                error_code="UNKNOWN_RESULT",
            )

            claimed = scheduler.claim_due(now)
            assert claimed is not None
            assert claimed.key == statuses.key
            completed = scheduler.complete(claimed.key, now=now)

            assert completed.next_run_at == now + timedelta(minutes=1)
    finally:
        database.close()


def test_search_pause_finishes_running_job_then_disables_and_resumes(
    settings: Settings,
) -> None:
    account_id, query_id = seed_search_query(settings)
    database = create_database(settings)
    started_at = datetime(2026, 7, 26, 13, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            scheduler.ensure_search_job(
                account_id=account_id,
                search_query_id=query_id,
                interval_minutes=120,
                now=started_at,
            )
            running = scheduler.claim_due(started_at)
            assert running is not None
            assert running.kind is AutomationJobKind.SEARCH
            assert running.state is AutomationJobState.RUNNING

        paused_at = started_at + timedelta(minutes=1)
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            settings_row = scheduler.pause_search(paused_at)
            still_running = scheduler.list_for_account(account_id)[0]

            assert settings_row.search_enabled is False
            assert still_running.state is AutomationJobState.RUNNING

        finished_at = paused_at + timedelta(minutes=1)
        with database.sessions.begin() as session:
            completed = AutomationSchedulerService(session).complete(
                running.key,
                {"found": 3},
                finished_at,
            )

            assert completed.state is AutomationJobState.DISABLED
            assert completed.next_run_at is None
            assert completed.last_success_at == finished_at
            assert completed.last_result == {"found": 3}

        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            assert scheduler.claim_due(finished_at + timedelta(days=1)) is None
            resumed = scheduler.resume_search(finished_at + timedelta(days=1))
            search = next(
                job
                for job in scheduler.list_for_account(account_id)
                if job.kind is AutomationJobKind.SEARCH
            )

            assert resumed.search_enabled is True
            assert search.state is AutomationJobState.WAITING
            assert search.next_run_at == finished_at + timedelta(days=1)
    finally:
        database.close()


def test_resource_saving_staggers_new_search_jobs_and_preserves_saved_schedule(
    settings: Settings,
) -> None:
    account_id, _query_id = seed_search_query(settings)
    database = create_database(settings)
    scheduled_at = datetime(2026, 7, 26, 14, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            direction = DirectionRepository(session).create(account_id, "Интеграции")
            DirectionRepository(session).add_query(
                direction.id,
                "Python API",
                schedule_minutes=90,
            )

        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            jobs = scheduler.ensure_configured_jobs(account_id, scheduled_at)
            searches = sorted(
                (job for job in jobs if job.kind is AutomationJobKind.SEARCH),
                key=lambda job: job.search_query_id or 0,
            )
            messages = next(job for job in jobs if job.kind is AutomationJobKind.MESSAGES)
            statuses = next(job for job in jobs if job.kind is AutomationJobKind.STATUSES)

            assert [job.interval_seconds for job in searches] == [240 * 60, 240 * 60]
            assert [job.next_run_at for job in searches] == [
                scheduled_at,
                scheduled_at + timedelta(minutes=5),
            ]
            assert messages.interval_seconds == 15 * 60
            assert statuses.interval_seconds == 60 * 60

        later = scheduled_at + timedelta(hours=1)
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            scheduler.ensure_configured_jobs(account_id, later)
            searches = sorted(
                (
                    job
                    for job in scheduler.list_for_account(account_id)
                    if job.kind is AutomationJobKind.SEARCH
                ),
                key=lambda job: job.search_query_id or 0,
            )
            assert [job.next_run_at for job in searches] == [
                scheduled_at,
                scheduled_at + timedelta(minutes=5),
            ]

        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            saved = scheduler.set_resource_saving_mode(False, later)
            jobs = scheduler.list_for_account(account_id)
            searches = sorted(
                (job for job in jobs if job.kind is AutomationJobKind.SEARCH),
                key=lambda job: job.search_query_id or 0,
            )

            assert saved.resource_saving_mode is False
            assert [job.interval_seconds for job in searches] == [120 * 60, 90 * 60]
            assert [job.next_run_at for job in searches] == [
                scheduled_at,
                scheduled_at + timedelta(minutes=5),
            ]
            assert (
                next(job for job in jobs if job.kind is AutomationJobKind.MESSAGES).interval_seconds
                == 5 * 60
            )
            assert (
                next(job for job in jobs if job.kind is AutomationJobKind.STATUSES).interval_seconds
                == 30 * 60
            )
    finally:
        database.close()


def test_scheduler_creates_jobs_only_for_configured_search_queries(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    scheduled_at = datetime(2026, 7, 26, 15, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Фоновая проверка")
            direction = DirectionRepository(session).create(account.id, "Python backend")
            configured = DirectionRepository(session).add_query(
                direction.id,
                "Python backend",
                schedule_minutes=120,
            )
            legacy_variant = DirectionRepository(session).add_query(
                direction.id,
                "Python backend",
                area="1",
                schedule_minutes=120,
            )
            scheduler = AutomationSchedulerService(session)
            scheduler.ensure_search_job(
                account_id=account.id,
                search_query_id=legacy_variant.id,
                interval_minutes=120,
                now=scheduled_at,
            )

        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            scheduler.ensure_configured_jobs(account.id, scheduled_at)
            searches = [
                job
                for job in scheduler.list_for_account(account.id)
                if job.kind is AutomationJobKind.SEARCH
            ]

            assert {job.search_query_id: job.state for job in searches} == {
                configured.id: AutomationJobState.WAITING,
                legacy_variant.id: AutomationJobState.DISABLED,
            }
    finally:
        database.close()
