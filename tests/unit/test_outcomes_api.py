from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationOutcomeModel,
    RecruiterMessageModel,
)
from hugin.domain.applications import ApplicationEventType, ApplicationState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, ApplicationRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository

pytestmark = pytest.mark.integration


def test_outcome_api_requires_evidence_keeps_revision_and_never_sends(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            other = AccountRepository(session).create("Other candidate")
            resume = ResumeRepository(session).upsert(account.id, "resume", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("api-result", "Python developer", "https://hh.ru/vacancy/api-result")
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id, vacancy.id, resume.id
            )
            ApplicationRepository(session).transition_state(
                application.id,
                ApplicationState.APPLIED,
                {"source": "hugin_send", "hh_status": "APPLIED"},
            )
            applied = session.scalar(
                select(ApplicationEventModel).where(
                    ApplicationEventModel.application_id == application.id,
                    ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                )
            )
            assert applied is not None
            applied.created_at = datetime.now(UTC) - timedelta(days=30)
        with TestClient(create_app(settings)) as client:
            path = f"/api/communications/applications/{application.id}/outcome"
            values = {
                "revision": 0,
                "interview_at": "2026-09-10T14:00:00+05:00",
                "interview_evidence": "Confirmed by recruiter in a phone call",
                "rejection_reason": "",
                "rejection_evidence": "",
            }
            assert client.put(path, json=values).status_code == 403
            headers = {"X-Hugin-Session": client.get("/api/session").json()["key"]}
            for invalid in (
                {"interview_evidence": " "},
                {"interview_at": "2026-09-10T14:00:00"},
                {"rejection_reason": "Experience"},
                {"rejection_evidence": "Said something"},
                {"revision": True},
                {"interview_evidence": "x" * 2001},
            ):
                assert (
                    client.put(path, headers=headers, json={**values, **invalid}).status_code == 422
                )
            assert (
                client.put(
                    path + f"?account_id={other.id}", headers=headers, json=values
                ).status_code
                == 404
            )
            assert client.get("/api/outcomes?account_id=99999").status_code == 404
            response = client.put(path, headers=headers, json=values)
            assert response.status_code == 200, response.text
            saved = response.json()["outcomes"][str(application.id)]
            assert saved["interview_at"] == "2026-09-10T09:00:00Z"
            assert client.put(path, headers=headers, json=values).status_code == 409
            values["revision"] = saved["revision"]
            assert client.put(path, headers=headers, json=values).status_code == 200
            result = client.get("/api/outcomes").json()
            assert result["sent_by_hugin"] == result["scheduled_interviews"] == 1
            assert result["confirmed_interview_invitations"] == 1
            assert result["invitations"] == 0
            assert result["cohorts"][0]["checked_after_window"] == 0
            assert result["confirmed_rejection_reasons"] == {}
            assert client.get(f"/api/outcomes?account_id={other.id}").json()["sent_by_hugin"] == 0
            saved_again = client.get("/api/communications").json()["outcomes"][str(application.id)]
            assert saved_again == saved
        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ApplicationOutcomeModel)) == 1
            assert session.scalar(select(func.count()).select_from(RecruiterMessageModel)) == 0
    finally:
        database.close()
