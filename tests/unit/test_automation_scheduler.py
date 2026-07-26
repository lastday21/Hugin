from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain import AutomationJobKind, AutomationJobState
from hugin.repositories import AccountRepository, DirectionRepository
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


def test_scheduler_uses_exact_intervals_and_does_not_catch_up(settings: Settings) -> None:
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

            assert messages.interval_seconds == 5 * 60
            assert statuses.interval_seconds == 30 * 60
            assert search.interval_seconds == 120 * 60
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
            assert completed.next_run_at == messages_finished_at + timedelta(minutes=5)

        statuses_finished_at = messages_finished_at + timedelta(seconds=10)
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            claimed = scheduler.claim_due(statuses_finished_at)
            assert claimed is not None
            assert claimed.kind is AutomationJobKind.STATUSES
            completed = scheduler.complete(claimed.key, now=statuses_finished_at)
            assert completed.next_run_at == statuses_finished_at + timedelta(minutes=30)

        search_finished_at = statuses_finished_at + timedelta(seconds=10)
        with database.sessions.begin() as session:
            scheduler = AutomationSchedulerService(session)
            claimed = scheduler.claim_due(search_finished_at)
            assert claimed is not None
            assert claimed.kind is AutomationJobKind.SEARCH
            completed = scheduler.complete(claimed.key, now=search_finished_at)
            assert completed.next_run_at == search_finished_at + timedelta(minutes=120)
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
            enabled = scheduler.enable(statuses.key, due_at + timedelta(days=1))
            unblocked = scheduler.unblock(messages.key, due_at + timedelta(days=1))
            assert enabled.state is AutomationJobState.WAITING
            assert unblocked.state is AutomationJobState.WAITING
            claimed = scheduler.claim_due(due_at + timedelta(days=1))
            assert claimed is not None
            assert claimed.kind is AutomationJobKind.MESSAGES
    finally:
        database.close()
