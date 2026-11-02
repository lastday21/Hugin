from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationEventModel, ApplicationTaskModel, HhAccountModel
from hugin.domain import ApplicationEventType, ApplicationState, TaskState, VacancyData
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    VacancyRepository,
)
from hugin.services.application_automation import ApplicationAutomationService
from hugin.workers.applications import ApplicationWorker
from tests.unit.test_application_automation_boundaries import queued_jobs
from tests.unit.test_workspace_api import request

pytestmark = pytest.mark.integration


def test_reapplication_review_api_preserves_guards_and_never_submits(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = create_database(settings)
    monkeypatch.setattr(
        ApplicationAutomationService, "_semantic_selection_current", lambda *_: True
    )
    monkeypatch.setattr(
        ApplicationAutomationService, "_application_selection_current", lambda *_: True
    )
    with database.sessions.begin() as session:
        previous, current = queued_jobs(session)
        account_model = session.get(HhAccountModel, current.application.account_id)
        assert account_model is not None
        account_model.external_id = "review-worker"
        vacancies = VacancyRepository(session)
        for job in (previous, current):
            vacancies.upsert(
                VacancyData(
                    job.vacancy.hh_id,
                    "Python developer",
                    job.vacancy.source_url,
                    employer_name="Employer",
                    description="Internal Python API."
                    if job is previous
                    else "Internal Python API, new requirements.",
                    details_fetched_at=datetime.now(UTC),
                )
            )
        vacancies.mark_duplicate(current.vacancy.id, previous.vacancy.id, 0.95)
        ApplicationRepository(session).transition_state(
            previous.application.id,
            ApplicationState.APPLIED,
            {"source": "hugin_send", "hh_status": "APPLIED"},
        )
        event = session.scalar(
            select(ApplicationEventModel).where(
                ApplicationEventModel.application_id == previous.application.id,
                ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
            )
        )
        assert event is not None
        event.created_at = datetime.now(UTC) - timedelta(days=60)
        account = AccountRepository(session).get(current.application.account_id)
        direction = DirectionRepository(session).get_for_account(
            account.id, current.application.direction_id or 0
        )
        ApplicationAutomationService(session)._prepare_tracked(
            account, direction, current.resume, current.direction_vacancy
        )
    app = create_app(settings)
    try:
        path = f"/api/queue/{current.task.id}/reapplication-review?account_id={account.id}"
        preview = request(app, "GET", path)
        assert preview.status_code == 200
        data = preview.json()
        assert data["can_review"] and data["code"] == "REPOST_CHANGED"
        assert data["previous"][0]["application_id"] == previous.application.id
        assert data["previous"][0]["source_url"] == previous.vacancy.source_url
        queue = request(app, "GET", f"/api/queue?account_id={account.id}").json()
        assert next(i for i in queue if i["task_id"] == current.task.id)[
            "reapplication_review_available"
        ]
        body = {
            "reason": "Подтверждённое изменение роли проверено по полному тексту вакансии.",
            "review_key": data["review_key"],
        }
        assert request(app, "POST", path, json=body).status_code == 403
        key = request(app, "GET", "/api/session").json()["key"]
        headers = {"X-Hugin-Session": key}
        assert (
            request(
                app, "GET", path.replace(f"account_id={account.id}", "account_id=999999")
            ).status_code
            == 404
        )
        assert (
            request(app, "POST", path, headers=headers, json={**body, "reason": "да"}).status_code
            == 422
        )
        assert (
            request(
                app, "POST", path, headers=headers, json={**body, "review_key": "0" * 64}
            ).status_code
            == 409
        )
        monkeypatch.setattr(
            ApplicationAutomationService, "_application_selection_current", lambda *_: False
        )
        assert not request(app, "GET", path).json()["can_review"]
        assert request(app, "POST", path, headers=headers, json=body).status_code == 409
        monkeypatch.setattr(
            ApplicationAutomationService, "_application_selection_current", lambda *_: True
        )
        with database.sessions.begin() as session:
            model = session.get(ApplicationTaskModel, current.task.id)
            assert model is not None
            model.state = TaskState.UNKNOWN_RESULT
        assert request(app, "POST", path, headers=headers, json=body).status_code == 409
        with database.sessions.begin() as session:
            model = session.get(ApplicationTaskModel, current.task.id)
            assert model is not None
            model.state = TaskState.REVIEW_REQUIRED
        saved = request(app, "POST", path, headers=headers, json=body)
        assert saved.status_code == 200 and saved.json()["allowed"]
        assert request(app, "POST", path, headers=headers, json=body).status_code == 200
        with database.sessions() as session:
            task = session.get(ApplicationTaskModel, current.task.id)
            assert task is not None and task.state is TaskState.REVIEW_REQUIRED
            events = ApplicationRepository(session).list_events(current.application.id)
            reviews = [e for e in events if e.payload.get("source") == "reapplication_review"]
            assert len(reviews) == 1
            review = reviews[0].payload["review"]
            assert isinstance(review, dict) and review["reason"] == body["reason"]
            assert not any(e.event_type.value == "APPLIED" for e in events)
        assert ApplicationWorker(settings, account_id=account.id).prepare_queue() == 1
        with database.sessions() as session:
            task = session.get(ApplicationTaskModel, current.task.id)
            assert task is not None and task.state is TaskState.RETRY_SCHEDULED
    finally:
        app.state.database.close()
        database.close()
