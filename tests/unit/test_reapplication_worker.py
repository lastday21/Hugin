from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationEventModel, HhAccountModel, RecruiterMessageModel
from hugin.domain import ApplicationEventType, ApplicationState, TaskState, VacancyData
from hugin.domain.content import MessageDirection, RecruiterMessageState
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    VacancyRepository,
)
from hugin.services.application_automation import ApplicationAutomationService
from hugin.workers.applications import ApplicationWorker
from tests.unit.test_application_automation_boundaries import queued_jobs

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("boundary", ["interval", "limit"])
def test_worker_reconsiders_repost_after_waiting_boundary(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    monkeypatch.setattr(
        ApplicationAutomationService, "_semantic_selection_current", lambda *_: True
    )
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            jobs = queued_jobs(session, count=3 if boundary == "limit" else 2)
            current = jobs[-1]
            account_model = session.get(HhAccountModel, current.application.account_id)
            assert account_model is not None
            account_model.external_id = "repost-worker"
            vacancies = VacancyRepository(session)
            apps = ApplicationRepository(session)
            confirmations = []
            for job in jobs:
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
                if job is not current:
                    apps.transition_state(
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
                    event.created_at = datetime.now(UTC) - timedelta(
                        days=60 if boundary == "limit" else 2
                    )
                    confirmations.append(event.id)
            account = AccountRepository(session).get(current.application.account_id)
            direction = DirectionRepository(session).get_for_account(
                account.id, current.application.direction_id or 0
            )
            automation = ApplicationAutomationService(session)
            assert automation._prepare_tracked(
                account, direction, current.resume, current.direction_vacancy
            ) == (0, 1)
            task = QueueTaskRepository(session).get(current.task.id)
            assert task.state is TaskState.REVIEW_REQUIRED
            assert task.last_error_code == (
                "REPOST_LIMIT" if boundary == "limit" else "REPOST_INTERVAL"
            )
        worker = ApplicationWorker(settings, account_id=account.id)
        assert worker.prepare_queue() == 0
        with database.sessions.begin() as session:
            session.add(
                RecruiterMessageModel(
                    application_id=jobs[0].application.id,
                    hh_id="retired-wrong-chat",
                    direction=MessageDirection.INCOMING,
                    body="Ошибочно связанное сообщение",
                    state=RecruiterMessageState.CANCELLED,
                    received_at=datetime.now(UTC),
                )
            )
            for event_id in confirmations:
                event = session.get(ApplicationEventModel, event_id)
                assert event is not None
                event.created_at = datetime.now(UTC) - timedelta(
                    days=91 if boundary == "limit" else 31
                )
        assert worker.prepare_queue() == 1
        with database.sessions() as session:
            task = QueueTaskRepository(session).get(current.task.id)
            assert task.state is TaskState.RETRY_SCHEDULED
            assert not any(
                event.event_type is ApplicationEventType.APPLIED
                for event in ApplicationRepository(session).list_events(current.application.id)
            )
    finally:
        database.close()
