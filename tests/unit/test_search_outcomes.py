from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationOutcomeModel,
    ApplicationStatusObservationModel,
)
from hugin.domain.applications import ApplicationEventType, ApplicationState
from hugin.domain.search_outcomes import StaleOutcomeError
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, ApplicationRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_outcomes import ApplicationOutcomeService
from hugin.services.search_outcomes import SearchOutcomeService

pytestmark = pytest.mark.integration


class OutcomeValues(TypedDict):
    interview_at: datetime | None
    interview_evidence: str
    rejection_reason: str
    rejection_evidence: str


def test_mature_cohorts_exclude_imports_deduplicate_and_show_observation_gaps(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            resume = ResumeRepository(session).upsert(account.id, "resume", "Python")

            def application(
                age: int, source: str, state: ApplicationState, version: str = "v1"
            ) -> ApplicationModel:
                vacancy = VacancyRepository(session).upsert(
                    VacancyData(
                        f"case-{age}-{source}", "Python developer", "https://hh.ru/vacancy/example"
                    )
                )
                record = ApplicationRepository(session).create_apply_intent(
                    account.id, vacancy.id, resume.id
                )
                app = session.get(ApplicationModel, record.id)
                assert app is not None
                app.state = state
                session.add(
                    ApplicationEventModel(
                        application_id=app.id,
                        event_type=ApplicationEventType.APPLIED,
                        created_at=now - timedelta(days=age),
                        payload={
                            "source": source,
                            "hh_status": "APPLIED",
                            "state": "APPLIED",
                            "rules_version": version,
                            "snapshot_missing": False,
                        },
                    )
                )
                return app

            invited = application(30, "hugin_send", ApplicationState.INVITED)
            for days in (25, 24):
                session.add(
                    ApplicationEventModel(
                        application_id=invited.id,
                        event_type=ApplicationEventType.STATE_CHANGED,
                        created_at=now - timedelta(days=days),
                        payload={"state": "INVITED"},
                    )
                )
            session.add(
                ApplicationEventModel(
                    application_id=invited.id,
                    event_type=ApplicationEventType.APPLIED,
                    created_at=now - timedelta(days=29),
                    payload={"source": "hugin_reconciliation", "hh_status": "APPLIED"},
                )
            )
            invited.status_checked_at = now
            session.add(
                ApplicationStatusObservationModel(
                    application_id=invited.id,
                    state=ApplicationState.INVITED,
                    checked_at=now,
                    recorded_at=now,
                )
            )
            for name in ("date", "duplicate-date"):
                session.add(
                    ApplicationOutcomeModel(
                        application_id=invited.id,
                        interview_at=now + timedelta(days=1),
                        interview_evidence=name,
                        recorded_at=now - timedelta(days=1),
                    )
                )
            rejected = application(21, "hugin_send", ApplicationState.REJECTED, "v2")
            rejected.status_checked_at = now - timedelta(days=1)
            session.add(
                ApplicationStatusObservationModel(
                    application_id=rejected.id,
                    state=ApplicationState.REJECTED,
                    checked_at=now - timedelta(days=1),
                    recorded_at=now - timedelta(days=1),
                )
            )
            application(14, "hugin_send", ApplicationState.VIEWED)
            application(13, "hugin_send", ApplicationState.APPLIED)
            application(31, "hh.ru", ApplicationState.INVITED)
            application(32, "legacy", ApplicationState.REJECTED)
            session.flush()
            result = SearchOutcomeService(session).snapshot(account.id, now=now)
            assert result.sent_by_hugin == 4
            assert result.imported_or_unattributed == 2
            assert result.invitations == 1
            assert result.scheduled_interviews == 1
            assert result.confirmed_rejection_reasons == {}
            fourteen, twenty_one = result.cohorts
            assert fourteen.applications == 3
            assert twenty_one.applications == 2
            assert fourteen.invitations == twenty_one.invitations == 1
            assert fourteen.scheduled_interviews == twenty_one.scheduled_interviews == 1
            assert fourteen.rejections == twenty_one.rejections == 1
            assert fourteen.without_decision == 1
            assert twenty_one.without_decision == 0
            assert fourteen.checked_after_window == 2
            assert twenty_one.checked_after_window == 1
            assert fourteen.checked_last_48_hours == 2
            versions = {row.rules_version: row for row in result.versions if row.age_days == 14}
            assert set(versions) == {"v1", "v2"}
            assert sum(row.applications for row in versions.values()) == fourteen.applications
            assert versions["v1"].applications == 2
            assert versions["v1"].invitations == 1
            assert versions["v1"].invitations_per_100 == 50
            assert (versions["v1"].youngest_days, versions["v1"].oldest_days) == (14, 30)
            assert versions["v2"].invitations_per_100 == 0
            assert all(row.with_outcome_context == 0 for row in result.versions)
            assert len(result.comparison_limitations) == 3
    finally:
        database.close()


def test_outcome_corrections_keep_history_isolate_accounts_and_survive_hh_sync(
    settings: Settings,
) -> None:
    from hugin.domain.content import MessageDirection
    from hugin.domain.hh_sync import HhChatMessageData, HhNegotiationData, HhNegotiationStatus
    from hugin.services.hh_sync import HhSynchronizationService

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            first = AccountRepository(session).create("First")
            other = AccountRepository(session).create("Other")
            resume = ResumeRepository(session).upsert(first.id, "first-resume", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("outcome", "Python", "https://hh.ru/vacancy/outcome")
            )
            app = ApplicationRepository(session).create_apply_intent(
                first.id, vacancy.id, resume.id
            )
            ApplicationRepository(session).transition_state(
                app.id, ApplicationState.APPLIED, {"source": "hugin_send", "hh_status": "APPLIED"}
            )
            service = ApplicationOutcomeService(session)
            values: OutcomeValues = dict(
                interview_at=datetime(2026, 9, 10, 9, tzinfo=UTC),
                interview_evidence="Recruiter confirmed 14:00 Yekaterinburg",
                rejection_reason="",
                rejection_evidence="",
            )
            saved = service.save(first.id, app.id, revision=0, **values)
            assert service.save(first.id, app.id, revision=saved.revision, **values) == saved
            with pytest.raises(StaleOutcomeError):
                service.save(first.id, app.id, revision=0, **values)
            with pytest.raises(LookupError):
                service.save(other.id, app.id, revision=saved.revision, **values)
            assert service.for_account(other.id) == {}
            sync = HhSynchronizationService(session)
            sync.synchronize_statuses(
                account_id=first.id,
                statuses=(HhNegotiationData("outcome", HhNegotiationStatus.INVITED, "Invitation"),),
            )
            message_result = sync.synchronize_messages(
                account_id=first.id,
                messages=(
                    HhChatMessageData(
                        "outcome",
                        "new-message",
                        MessageDirection.INCOMING,
                        "Приглашаем на собеседование",
                    ),
                ),
            )
            assert message_result["matched"] == message_result["created"] == 1
            assert service.for_account(first.id)[app.id] == saved
            assert SearchOutcomeService(session).snapshot(first.id).scheduled_interviews == 1
            rejected_values: OutcomeValues = {
                **values,
                "rejection_reason": "Experience",
                "rejection_evidence": "Recruiter said five years are required",
            }
            revised = service.save(
                first.id,
                app.id,
                revision=saved.revision,
                **rejected_values,
            )
            assert revised.revision > saved.revision
            snapshot = SearchOutcomeService(session).snapshot(first.id)
            assert snapshot.confirmed_rejection_reasons == {"Experience": 1}
            assert snapshot.scheduled_interviews == 1
            cleared = service.save(
                first.id,
                app.id,
                revision=revised.revision,
                interview_at=None,
                interview_evidence="",
                rejection_reason="",
                rejection_evidence="",
            )
            assert cleared.revision > revised.revision
            snapshot = SearchOutcomeService(session).snapshot(first.id)
            assert snapshot.scheduled_interviews == 0
            assert snapshot.confirmed_rejection_reasons == {}
            assert session.scalar(select(func.count()).select_from(ApplicationOutcomeModel)) == 3
            assert service.for_account(first.id, recorded_before=saved.recorded_at)[app.id] == saved
    finally:
        database.close()


def test_empty_sample_has_no_inferred_cause(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            result = SearchOutcomeService(session).snapshot(account.id)
            assert result.sent_by_hugin == 0
            assert all(item.applications == 0 for item in result.cohorts)
            assert result.confirmed_rejection_reasons == {}
            assert len(result.limitations) == 5
    finally:
        database.close()


def test_confirmed_invitation_without_date_is_a_result_and_corrections_are_historical(
    settings: Settings,
) -> None:
    database = create_database(settings)
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            resume = ResumeRepository(session).upsert(account.id, "resume", "Python")
            ids = []
            for index, source in enumerate(("hugin_send", "hugin_send", "hh.ru")):
                vacancy = VacancyRepository(session).upsert(
                    VacancyData(str(index), "Python", f"https://hh.ru/vacancy/{index}")
                )
                app = ApplicationRepository(session).create_apply_intent(
                    account.id, vacancy.id, resume.id
                )
                ids.append(app.id)
                session.add(
                    ApplicationEventModel(
                        application_id=app.id,
                        event_type=ApplicationEventType.APPLIED,
                        created_at=now - timedelta(days=30),
                        payload={"source": source, "hh_status": "APPLIED", "rules_version": "v1"},
                    )
                )
            evidence = "Работодатель пригласил на собеседование по телефону; дату выбираем."
            service = ApplicationOutcomeService(session)
            for app_id in (ids[0], ids[2]):
                saved = service.save(
                    account.id,
                    app_id,
                    revision=0,
                    interview_at=None,
                    interview_evidence=evidence,
                    rejection_reason="",
                    rejection_evidence="",
                )
                model = session.get(ApplicationOutcomeModel, saved.revision)
                assert model is not None
                model.recorded_at = now - timedelta(days=1)
            session.flush()
            result = SearchOutcomeService(session).snapshot(account.id, now=now)
            assert result.sent_by_hugin == 2
            assert result.imported_or_unattributed == 1
            assert result.confirmed_interview_invitations == 1
            assert result.invitations == result.scheduled_interviews == 0
            for cohort in result.cohorts:
                assert cohort.applications == 2
                assert cohort.confirmed_interview_invitations == 1
                assert cohort.without_decision == 1
            assert all(row.confirmed_interview_invitations_per_100 == 50 for row in result.versions)
            before = SearchOutcomeService(session).snapshot(account.id, now=now - timedelta(days=2))
            assert before.confirmed_interview_invitations == 0
            current = service.for_account(account.id)[ids[0]]
            corrected = service.save(
                account.id,
                ids[0],
                revision=current.revision,
                interview_at=None,
                interview_evidence="",
                rejection_reason="",
                rejection_evidence="",
            )
            assert corrected.revision > current.revision
            assert (
                SearchOutcomeService(session).snapshot(account.id).confirmed_interview_invitations
                == 0
            )
            assert (
                SearchOutcomeService(session)
                .snapshot(account.id, now=now)
                .confirmed_interview_invitations
                == 1
            )
    finally:
        database.close()
