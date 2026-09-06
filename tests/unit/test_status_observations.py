from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationStatusObservationModel,
)
from hugin.domain.applications import ApplicationEventType, ApplicationState
from hugin.domain.hh_sync import HhNegotiationData, HhNegotiationStatus
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, ApplicationRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.hh_sync import HhSynchronizationService
from hugin.services.search_outcomes import SearchOutcomeService

pytestmark = pytest.mark.integration


def _sent_application(session: Session, now: datetime) -> tuple[int, ApplicationModel]:
    account = AccountRepository(session).create("Candidate")
    resume = ResumeRepository(session).upsert(account.id, "resume", "Python")
    vacancy = VacancyRepository(session).upsert(
        VacancyData("history", "Python", "https://hh.ru/vacancy/history")
    )
    record = ApplicationRepository(session).create_apply_intent(account.id, vacancy.id, resume.id)
    application = session.get(ApplicationModel, record.id)
    assert application is not None
    application.state = ApplicationState.APPLIED
    session.add(
        ApplicationEventModel(
            application_id=record.id,
            event_type=ApplicationEventType.APPLIED,
            created_at=now - timedelta(days=30),
            payload={"source": "hugin_send", "hh_status": "APPLIED", "state": "APPLIED"},
        )
    )
    session.flush()
    return account.id, application


def test_history_keeps_rejection_after_invitation_and_closed_vacancy(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application = _sent_application(session, datetime.now(UTC))
            service = HhSynchronizationService(session)
            for state in (
                HhNegotiationStatus.INVITED,
                HhNegotiationStatus.REJECTED,
                HhNegotiationStatus.CLOSED,
            ):
                service.synchronize_statuses(
                    account_id=account_id,
                    statuses=(HhNegotiationData("history", state, state.value),),
                )
            result = SearchOutcomeService(session).snapshot(account_id)
            assert result.invitations == 1
            assert all(cohort.rejections == 1 for cohort in result.cohorts)
            assert all(cohort.without_decision == 0 for cohort in result.cohorts)
            assert all(cohort.checked_after_window == 1 for cohort in result.cohorts)
            assert (
                session.scalar(select(func.count()).select_from(ApplicationStatusObservationModel))
                == 3
            )
            assert application.status_checked_at is not None
    finally:
        database.close()


def test_historical_snapshot_ignores_later_decisions_and_keeps_prior_observations(
    settings: Settings,
) -> None:
    database = create_database(settings)
    cutoff = datetime.now(UTC) - timedelta(days=2)
    try:
        with database.sessions.begin() as session:
            account_id, application = _sent_application(session, cutoff)
            session.add(
                ApplicationStatusObservationModel(
                    application_id=application.id,
                    state=ApplicationState.VIEWED,
                    checked_at=cutoff - timedelta(hours=1),
                    recorded_at=cutoff - timedelta(minutes=59),
                )
            )
            session.flush()
            before = SearchOutcomeService(session).snapshot(account_id, now=cutoff)
            HhSynchronizationService(session).synchronize_statuses(
                account_id=account_id,
                statuses=(HhNegotiationData("history", HhNegotiationStatus.REJECTED, "Rejected"),),
            )
            after = SearchOutcomeService(session).snapshot(account_id, now=cutoff)
            assert before == after
            assert before.cohorts[0].rejections == 0
            assert before.cohorts[0].checked_after_window == 1
            assert SearchOutcomeService(session).snapshot(account_id).cohorts[0].rejections == 1
    finally:
        database.close()


def test_repeated_and_out_of_order_checks_do_not_duplicate_or_replace_history(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            now = datetime.now(UTC)
            account_id, application = _sent_application(session, now)
            service = HhSynchronizationService(session)
            for checked_at in (now, now, now - timedelta(days=1)):
                service.synchronize_statuses(
                    account_id=account_id,
                    statuses=(HhNegotiationData("history", HhNegotiationStatus.APPLIED, "Sent"),),
                    checked_at=checked_at,
                )
            assert (
                session.scalar(select(func.count()).select_from(ApplicationStatusObservationModel))
                == 2
            )
            assert application.status_checked_at == now
            other = AccountRepository(session).create("Other")
            assert SearchOutcomeService(session).snapshot(other.id).sent_by_hugin == 0
    finally:
        database.close()


def test_late_recorded_status_does_not_appear_in_earlier_report(settings: Settings) -> None:
    database = create_database(settings)
    cutoff = datetime.now(UTC) - timedelta(days=2)
    try:
        with database.sessions.begin() as session:
            account_id, application = _sent_application(session, cutoff)
            application.state = ApplicationState.REJECTED
            application.status_checked_at = cutoff - timedelta(hours=1)
            session.add(
                ApplicationStatusObservationModel(
                    application_id=application.id,
                    state=ApplicationState.REJECTED,
                    checked_at=cutoff - timedelta(hours=1),
                    recorded_at=cutoff + timedelta(hours=1),
                )
            )
            session.flush()
            earlier = SearchOutcomeService(session).snapshot(account_id, now=cutoff)
            assert earlier.cohorts[0].rejections == 0
            assert earlier.cohorts[0].checked_after_window == 0
            later = SearchOutcomeService(session).snapshot(account_id)
            assert later.cohorts[0].rejections == 1
            assert later.cohorts[0].checked_after_window == 1
    finally:
        database.close()


def test_delayed_invitation_adds_history_without_replacing_newer_rejection(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        now = datetime.now(UTC)
        with database.sessions.begin() as session:
            account_id, application = _sent_application(session, now)
            service = HhSynchronizationService(session)
            service.synchronize_statuses(
                account_id=account_id,
                statuses=(HhNegotiationData("history", HhNegotiationStatus.REJECTED, "Rejected"),),
                checked_at=now,
            )
            cutoff = datetime.now(UTC)
            before = SearchOutcomeService(session).snapshot(account_id, now=cutoff)
            service.synchronize_statuses(
                account_id=account_id,
                statuses=(HhNegotiationData("history", HhNegotiationStatus.INVITED, "Invited"),),
                checked_at=now - timedelta(days=1),
            )
            assert application.state is ApplicationState.REJECTED
            assert application.status_checked_at == now
            assert SearchOutcomeService(session).snapshot(account_id, now=cutoff) == before
            assert before.invitations == 0
            after = SearchOutcomeService(session).snapshot(account_id)
            assert after.invitations == 1
            assert after.cohorts[0].rejections == 1
            assert after.cohorts[0].without_decision == 0
            assert (
                session.scalar(select(func.count()).select_from(ApplicationStatusObservationModel))
                == 2
            )
    finally:
        database.close()


def test_distinct_statuses_at_same_time_survive_replayed_response(settings: Settings) -> None:
    database = create_database(settings)
    try:
        now = datetime.now(UTC)
        with database.sessions.begin() as session:
            account_id, _application = _sent_application(session, now)
            service = HhSynchronizationService(session)
            for state in (
                HhNegotiationStatus.INVITED,
                HhNegotiationStatus.REJECTED,
                HhNegotiationStatus.REJECTED,
                HhNegotiationStatus.INVITED,
            ):
                service.synchronize_statuses(
                    account_id=account_id,
                    statuses=(HhNegotiationData("history", state, state.value),),
                    checked_at=now,
                )
            assert (
                session.scalar(select(func.count()).select_from(ApplicationStatusObservationModel))
                == 2
            )
            result = SearchOutcomeService(session).snapshot(account_id)
            assert result.invitations == 1
            assert result.cohorts[0].rejections == 1
            assert result.cohorts[0].checked_after_window == 1
            assert result.cohorts[0].without_decision == 0
    finally:
        database.close()


def test_concurrent_older_response_waits_and_preserves_latest_state(settings: Settings) -> None:
    database = create_database(settings)
    loaded = Event()
    synchronize = Event()
    worker_pid: list[int] = []
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            account_id, application = _sent_application(session, now)
            application_id = application.id

        def older_response() -> None:
            with database.sessions.begin() as session:
                cached = session.get(ApplicationModel, application_id)
                assert cached is not None
                assert cached.state in {ApplicationState.APPLIED}
                worker_pid.append(int(session.scalar(text("SELECT pg_backend_pid()")) or 0))
                loaded.set()
                assert synchronize.wait(5), "Newer response did not reach the synchronization gate"
                HhSynchronizationService(session).synchronize_statuses(
                    account_id=account_id,
                    statuses=(
                        HhNegotiationData("history", HhNegotiationStatus.INVITED, "Invited"),
                    ),
                    checked_at=now - timedelta(days=1),
                )
                assert cached.state is ApplicationState.REJECTED
                assert cached.status_checked_at == now

        with ThreadPoolExecutor(max_workers=1) as executor:
            older = executor.submit(older_response)
            assert loaded.wait(5), "Older response did not read the initial state"
            with database.sessions.begin() as newer_session:
                HhSynchronizationService(newer_session).synchronize_statuses(
                    account_id=account_id,
                    statuses=(
                        HhNegotiationData("history", HhNegotiationStatus.REJECTED, "Rejected"),
                    ),
                    checked_at=now,
                )
                synchronize.set()
                deadline = monotonic() + 5
                waiting_for_lock = False
                while monotonic() < deadline:
                    with database.sessions() as inspection:
                        waiting_for_lock = (
                            inspection.scalar(
                                text(
                                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity "
                                    "WHERE pid = :pid"
                                ),
                                {"pid": worker_pid[0]},
                            )
                            is True
                        )
                    if waiting_for_lock or older.done():
                        break
                    sleep(0.01)
                assert waiting_for_lock, "Older response did not wait for the newer transaction"
            older.result(timeout=5)

        with database.sessions() as session:
            saved = session.get(ApplicationModel, application_id)
            assert saved is not None and saved.state is ApplicationState.REJECTED
            assert saved.status_checked_at == now
            assert (
                session.scalar(select(func.count()).select_from(ApplicationStatusObservationModel))
                == 2
            )
            result = SearchOutcomeService(session).snapshot(account_id)
            assert result.invitations == 1
            assert result.cohorts[0].rejections == 1
            assert result.cohorts[0].without_decision == 0
    finally:
        synchronize.set()
        database.close()
