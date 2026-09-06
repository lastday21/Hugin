from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationEventModel, ApplicationOutcomeModel
from hugin.domain.applications import ApplicationEventType
from hugin.domain.search_outcomes import ApplicationOutcome, StaleOutcomeError
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, ApplicationRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_outcomes import ApplicationOutcomeService
from hugin.services.search_outcomes import SearchOutcomeService

pytestmark = pytest.mark.integration


def test_confirmed_invitation_totals_keep_dates_sources_and_versions_separate(
    settings: Settings,
) -> None:
    database = create_database(settings)
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            other = AccountRepository(session).create("Other candidate")
            resume = ResumeRepository(session).upsert(account.id, "resume", "Python")
            other_resume = ResumeRepository(session).upsert(other.id, "other", "Python")

            def add(
                key: str,
                age: int,
                version: str | None,
                *,
                evidence: bool = False,
                date: bool = False,
                source: str = "hugin_send",
                hh_invited: bool = False,
                rejected: bool = False,
                foreign: bool = False,
            ) -> int:
                vacancy = VacancyRepository(session).upsert(
                    VacancyData(key, "Python", f"https://hh.ru/vacancy/{key}")
                )
                record = ApplicationRepository(session).create_apply_intent(
                    other.id if foreign else account.id,
                    vacancy.id,
                    other_resume.id if foreign else resume.id,
                )
                session.add(
                    ApplicationEventModel(
                        application_id=record.id,
                        event_type=ApplicationEventType.APPLIED,
                        created_at=now - timedelta(days=age),
                        payload={
                            "source": source,
                            "hh_status": "APPLIED",
                            "rules_version": version,
                        },
                    )
                )
                if hh_invited:
                    session.add(
                        ApplicationEventModel(
                            application_id=record.id,
                            event_type=ApplicationEventType.STATE_CHANGED,
                            created_at=now - timedelta(days=2),
                            payload={"state": "INVITED"},
                        )
                    )
                if evidence or date or rejected:
                    session.add(
                        ApplicationOutcomeModel(
                            application_id=record.id,
                            interview_evidence="Employer offered an interview" if evidence else "",
                            interview_at=now + timedelta(days=3) if date else None,
                            rejection_reason="Position filled" if rejected else "",
                            rejection_evidence="Employer email" if rejected else "",
                            recorded_at=now - timedelta(hours=1),
                        )
                    )
                return record.id

            duplicated = add("hh-only", 30, "v1", hh_invited=True)
            add("no-date", 21, "v1", evidence=True)
            add("scheduled", 14, "v2", evidence=True, date=True)
            add("young", 13, "v2", evidence=True)
            add("then-rejected", 35, "v2", evidence=True, rejected=True)
            add("silent", 40, None)
            add("import", 40, "v1", source="hh.ru", evidence=True, date=True)
            add("foreign", 40, "v2", evidence=True, date=True, foreign=True)
            session.add(
                ApplicationEventModel(
                    application_id=duplicated,
                    event_type=ApplicationEventType.APPLIED,
                    created_at=now - timedelta(days=29),
                    payload={
                        "source": "hugin_reconciliation",
                        "hh_status": "APPLIED",
                        "rules_version": "v2",
                    },
                )
            )
            session.flush()
            result = SearchOutcomeService(session).snapshot(account.id, now=now)
            assert result.sent_by_hugin == 6
            assert result.imported_or_unattributed == 1
            assert result.confirmed_interview_invitations == 4
            assert result.scheduled_interviews == 1
            assert result.invitations == 1
            cohorts = {item.age_days: item for item in result.cohorts}
            assert cohorts[14].applications == 5
            assert cohorts[14].confirmed_interview_invitations == 3
            assert cohorts[14].scheduled_interviews == 1
            assert cohorts[14].without_decision == 1
            assert cohorts[14].rejections == 1
            assert cohorts[21].applications == 4
            assert cohorts[21].confirmed_interview_invitations == 2
            assert cohorts[21].scheduled_interviews == 0
            groups = {(item.age_days, item.rules_version): item for item in result.versions}
            assert groups[14, "v1"].applications == 2
            assert groups[14, "v1"].confirmed_interview_invitations_per_100 == 50
            assert groups[14, "v2"].applications == 2
            assert groups[14, "v2"].confirmed_interview_invitations_per_100 == 100
            assert groups[14, None].confirmed_interview_invitations_per_100 == 0
            assert groups[21, "v2"].applications == 1
            for age, cohort in cohorts.items():
                age_groups = [item for item in result.versions if item.age_days == age]
                assert sum(item.applications for item in age_groups) == cohort.applications
                assert (
                    sum(item.confirmed_interview_invitations for item in age_groups)
                    == cohort.confirmed_interview_invitations
                )
            assert (
                SearchOutcomeService(session)
                .snapshot(other.id, now=now)
                .confirmed_interview_invitations
                == 1
            )
    finally:
        database.close()


def test_clearing_date_keeps_invitation_and_clearing_evidence_changes_only_later_slices(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            resume = ResumeRepository(session).upsert(account.id, "resume", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("history", "Python", "https://hh.ru/vacancy/history")
            )
            record = ApplicationRepository(session).create_apply_intent(
                account.id, vacancy.id, resume.id
            )
            session.add(
                ApplicationEventModel(
                    application_id=record.id,
                    event_type=ApplicationEventType.APPLIED,
                    created_at=datetime.now(UTC) - timedelta(days=30),
                    payload={"source": "hugin_send", "hh_status": "APPLIED"},
                )
            )
        evidence = "Recruiter proposed an interview by telephone"
        revisions: list[ApplicationOutcome] = []
        for date, description in (
            (None, evidence),
            (datetime.now(UTC) + timedelta(days=1), evidence),
            (None, evidence),
            (None, ""),
        ):
            with database.sessions.begin() as session:
                service = ApplicationOutcomeService(session)
                saved = service.save(
                    account.id,
                    record.id,
                    revision=revisions[-1].revision if revisions else 0,
                    interview_at=date,
                    interview_evidence=description,
                    rejection_reason="",
                    rejection_evidence="",
                )
                revisions.append(saved)
        with database.sessions() as session:
            for row, counts in zip(revisions, ((1, 0), (1, 1), (1, 0), (0, 0)), strict=True):
                result = SearchOutcomeService(session).snapshot(account.id, now=row.recorded_at)
                assert (
                    result.confirmed_interview_invitations,
                    result.scheduled_interviews,
                ) == counts
                assert result.cohorts[0].confirmed_interview_invitations == counts[0]
                assert result.cohorts[1].confirmed_interview_invitations == counts[0]
            assert session.scalar(select(func.count()).select_from(ApplicationOutcomeModel)) == 4
            with pytest.raises(StaleOutcomeError):
                ApplicationOutcomeService(session).save(
                    account.id,
                    record.id,
                    revision=revisions[0].revision,
                    interview_at=None,
                    interview_evidence=evidence,
                    rejection_reason="",
                    rejection_evidence="",
                )
    finally:
        database.close()
