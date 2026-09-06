from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationEventModel, ApplicationModel, RecruiterMessageModel
from hugin.domain.applications import ApplicationEventType, ApplicationState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, ApplicationRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository

pytestmark = pytest.mark.integration


def _application(session: Session, account_id: int, name: str) -> ApplicationModel:
    resume = ResumeRepository(session).upsert(account_id, f"resume-{name}", "Python")
    vacancy = VacancyRepository(session).upsert(
        VacancyData(name, name, f"https://hh.ru/vacancy/{name}", employer_name="Company")
    )
    record = ApplicationRepository(session).create_apply_intent(account_id, vacancy.id, resume.id)
    application = session.get(ApplicationModel, record.id)
    assert application is not None
    return application


def test_sent_outcome_choices_include_silent_and_rejected_but_require_owned_confirmation(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            other = AccountRepository(session).create("Other candidate")
            silent = _application(session, account.id, "silent")
            rejected = _application(session, account.id, "rejected")
            pending = _application(session, account.id, "pending")
            foreign = _application(session, other.id, "foreign")
            state_without_event = _application(session, account.id, "unconfirmed")
            state_without_event.state = ApplicationState.APPLIED
            applications = ApplicationRepository(session)
            for application in (silent, rejected, foreign):
                applications.transition_state(
                    application.id, ApplicationState.APPLIED, {"source": "hugin_send"}
                )
            applications.transition_state(rejected.id, ApplicationState.REJECTED)
            confirmed_at = datetime.now(UTC) - timedelta(days=20)
            original = session.scalar(
                select(ApplicationEventModel).where(
                    ApplicationEventModel.application_id == silent.id,
                    ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                )
            )
            assert original is not None
            original.created_at = confirmed_at
            session.add(
                ApplicationEventModel(
                    application_id=silent.id,
                    event_type=ApplicationEventType.APPLIED,
                    payload={"source": "hugin_reconciliation"},
                    created_at=confirmed_at + timedelta(days=1),
                )
            )
        with TestClient(create_app(settings)) as client:
            response = client.get(f"/api/communications?account_id={account.id}")
            assert response.status_code == 200, response.text
            content = response.json()
            assert content["conversations"] == content["invitations"] == []
            choices = content["sent_applications"]
            assert [item["application_id"] for item in choices] == [rejected.id, silent.id]
            assert choices[0]["state"] == "REJECTED"
            assert choices[1]["vacancy_title"] == "silent"
            assert datetime.fromisoformat(choices[1]["confirmed_at"]) == confirmed_at
            excluded = {pending.id, foreign.id, state_without_event.id}
            assert not excluded.intersection(item["application_id"] for item in choices)
            headers = {"X-Hugin-Session": client.get("/api/session").json()["key"]}
            saved = client.put(
                f"/api/communications/applications/{silent.id}/outcome?account_id={account.id}",
                headers=headers,
                json={
                    "revision": 0,
                    "interview_at": None,
                    "interview_evidence": "",
                    "rejection_reason": "Role already filled",
                    "rejection_evidence": "Recruiter confirmed by phone",
                },
            )
            assert saved.status_code == 200, saved.text
            assert (
                saved.json()["outcomes"][str(silent.id)]["rejection_reason"]
                == "Role already filled"
            )
        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(RecruiterMessageModel)) == 0
    finally:
        database.close()
