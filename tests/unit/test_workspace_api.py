from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    CoverLetterModel,
    IncidentModel,
    InvitationModel,
    RecruiterMessageModel,
)
from hugin.domain import (
    ApplicationState,
    CoverLetterState,
    HhScreeningField,
    HhScreeningForm,
    IncidentSeverity,
    InvitationState,
    MessageDirection,
    RecruiterMessageState,
    TaskState,
    VacancyData,
    VacancyState,
)
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.services.screening_forms import ScreeningDraftService

pytestmark = pytest.mark.integration


def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json: object | None = None,
) -> Response:
    async def send() -> Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers, json=json)

    return asyncio.run(send())


def seed_workspace(settings: Settings) -> tuple[int, str, str]:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тимур", "workspace-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-it", "Python")
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "ИТ")
            query = directions.add_query(direction.id, "Python разработчик")

            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="ui-101",
                    title="Python разработчик",
                    source_url="https://hh.ru/vacancy/ui-101",
                    employer_name="Пример",
                    description="Разработка серверной части",
                    experience="3–6 лет",
                    employment="Полная занятость",
                    work_format="Удалённо",
                    key_skills=("Python", "PostgreSQL"),
                    region="Екатеринбург",
                    address="Екатеринбург",
                    salary_from=Decimal("120000"),
                    salary_to=Decimal("180000"),
                    salary_currency="RUR",
                    details_fetched_at=datetime(2026, 7, 22, 7, 30, tzinfo=UTC),
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=82,
                details={"reasons": ["подходит основной стек"]},
                rules_version="ui-test",
            )
            directions.record_discovery(
                direction_id=direction.id,
                search_query_id=query.id,
                vacancy_id=vacancy.id,
                query_text="Python разработчик",
                region="Екатеринбург",
            )
            applications = ApplicationRepository(session)
            application = applications.create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(
                application.id,
                priority_score=82,
                scheduled_at=datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
            )
            session.add(
                CoverLetterModel(
                    application_id=application.id,
                    vacancy_id=vacancy.id,
                    direction_id=direction.id,
                    resume_id=resume.id,
                    text="Здравствуйте! Готов обсудить задачи команды.",
                    instruction_version="ui-test",
                    model_name="test",
                    state=CoverLetterState.READY,
                )
            )
            ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "motivation",
                            "Почему вам интересна вакансия?",
                            "textarea",
                            is_required=True,
                        ),
                    )
                ),
            )
            session.add_all(
                (
                    RecruiterMessageModel(
                        application_id=application.id,
                        hh_id="message-1",
                        direction=MessageDirection.INCOMING,
                        body="Приглашаем познакомиться",
                        state=RecruiterMessageState.RECEIVED,
                    ),
                    InvitationModel(
                        application_id=application.id,
                        hh_id="invite-1",
                        title="Знакомство с командой",
                        state=InvitationState.RECEIVED,
                    ),
                    IncidentModel(
                        code="UI_TEST_WARNING",
                        severity=IncidentSeverity.WARNING,
                        message="Проверочное предупреждение",
                    ),
                )
            )

            rejected = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="ui-202",
                    title="Разработчик Java",
                    source_url="https://hh.ru/vacancy/ui-202",
                    employer_name="Другая компания",
                )
            )
            directions.track_vacancy(direction.id, rejected.id)
            directions.apply_rules(
                direction.id,
                rejected.id,
                state=VacancyState.FILTERED_OUT,
                score=25,
                details={"reasons": ["другой основной язык"]},
                rules_version="ui-test",
            )

            applied = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="ui-303",
                    title="Серверный разработчик",
                    source_url="https://hh.ru/vacancy/ui-303",
                )
            )
            applied_application = applications.create_apply_intent(
                account.id,
                applied.id,
                resume.id,
                direction.id,
            )
            applications.transition_state(
                applied_application.id,
                ApplicationState.APPLIED,
                {"hh_status": ApplicationState.APPLIED.value},
            )
            return account.id, vacancy.hh_id, rejected.hh_id
    finally:
        database.close()


def test_workspace_endpoints_return_real_data_and_protect_changes(settings: Settings) -> None:
    account_id, vacancy_id, rejected_id = seed_workspace(settings)
    app = create_app(settings)
    try:
        dashboard = request(app, "GET", f"/api/dashboard?account_id={account_id}")
        assert dashboard.status_code == 200
        dashboard_data = dashboard.json()
        assert dashboard_data["account_label"] == "Тимур"
        assert dashboard_data["applied_today"] == 1
        assert dashboard_data["remaining_today"] == 24
        assert dashboard_data["delay_min_seconds"] == 30
        assert dashboard_data["delay_max_seconds"] == 60
        assert dashboard_data["task_counts"] == {TaskState.PENDING.value: 1}
        assert dashboard_data["pending_forms"] == 1
        assert dashboard_data["ready_letters"] == 1
        assert dashboard_data["rejected_vacancies"] == 1
        assert dashboard_data["new_messages"] == 1
        assert dashboard_data["invitations"] == 1
        assert dashboard_data["directions"][0]["queued"] == 1
        assert dashboard_data["directions"][0]["rejected"] == 1
        assert dashboard_data["incidents"][0]["code"] == "UI_TEST_WARNING"

        queue = request(app, "GET", f"/api/queue?account_id={account_id}&limit=10")
        assert queue.status_code == 200
        assert queue.json()[0] == {
            "task_id": queue.json()[0]["task_id"],
            "vacancy_id": vacancy_id,
            "title": "Python разработчик",
            "company": "Пример",
            "region": "Екатеринбург",
            "source_url": f"https://hh.ru/vacancy/{vacancy_id}",
            "resume_title": "Python",
            "direction": "ИТ",
            "state": "PENDING",
            "priority": 82.0,
            "scheduled_at": "2026-07-22T08:00:00Z",
            "last_error": None,
            "letter_state": "READY",
            "form_state": "INPUT_REQUIRED",
        }

        forms = request(app, "GET", f"/api/forms?account_id={account_id}")
        assert forms.status_code == 200
        assert forms.json()[0]["vacancy_id"] == vacancy_id
        assert forms.json()[0]["unanswered_count"] == 1
        assert forms.json()[0]["questions"][0]["field_key"] == "motivation"

        rejected = request(app, "GET", f"/api/rejected?account_id={account_id}")
        assert rejected.status_code == 200
        assert rejected.json()[0]["vacancy_id"] == rejected_id
        assert rejected.json()[0]["reasons"] == ["другой основной язык"]

        card = request(app, "GET", f"/api/vacancies/{vacancy_id}?account_id={account_id}")
        assert card.status_code == 200
        card_data = card.json()
        assert card_data["salary"] == "от 120 000 до 180 000 ₽"
        assert card_data["skills"] == ["Python", "PostgreSQL"]
        assert card_data["discoveries"] == ["Python разработчик — Екатеринбург"]
        assert card_data["cover_letter"].startswith("Здравствуйте!")
        assert card_data["questions"][0]["answer"] is None
        assert card_data["events"][0]["event_type"] == "APPLY_INTENT"

        assert request(app, "POST", "/api/queue/pause").status_code == 403
        session_key = request(app, "GET", "/api/session").json()["key"]
        headers = {"X-Hugin-Session": session_key}
        paused = request(app, "POST", "/api/queue/pause", headers=headers)
        assert paused.status_code == 200
        assert paused.json()["state"] == "PAUSED"
        resumed = request(app, "POST", "/api/queue/resume", headers=headers)
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "RUNNING"

        values = {
            "daily_limit": 51,
            "delay_min_seconds": 36,
            "delay_max_seconds": 72,
        }
        assert (
            request(app, "PUT", "/api/queue/settings", json=values).status_code
            == 403
        )
        saved = request(
            app,
            "PUT",
            "/api/queue/settings",
            headers=headers,
            json=values,
        )
        assert saved.status_code == 200
        assert saved.json() == values
        updated_dashboard = request(
            app,
            "GET",
            f"/api/dashboard?account_id={account_id}",
        ).json()
        assert updated_dashboard["daily_limit"] == 51
        assert updated_dashboard["delay_min_seconds"] == 36
        assert updated_dashboard["delay_max_seconds"] == 72
        assert updated_dashboard["remaining_today"] == 50

        invalid_limit = request(
            app,
            "PUT",
            "/api/queue/settings",
            headers=headers,
            json={**values, "daily_limit": 24},
        )
        assert invalid_limit.status_code == 422
        invalid_delay = request(
            app,
            "PUT",
            "/api/queue/settings",
            headers=headers,
            json={
                "daily_limit": 25,
                "delay_min_seconds": 60,
                "delay_max_seconds": 30,
            },
        )
        assert invalid_delay.status_code == 422
        assert "не может быть меньше" in invalid_delay.json()["detail"]
    finally:
        app.state.database.close()


def test_workspace_endpoints_report_missing_entities(settings: Settings) -> None:
    account_id, _vacancy_id, _rejected_id = seed_workspace(settings)
    app = create_app(settings)
    try:
        for path in (
            "/api/dashboard?account_id=99999",
            "/api/queue?account_id=99999",
            "/api/rejected?account_id=99999",
            f"/api/vacancies/missing?account_id={account_id}",
        ):
            response = request(app, "GET", path)
            assert response.status_code == 404
    finally:
        app.state.database.close()
