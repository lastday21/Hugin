from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationModel,
    ApplicationSettingsModel,
    ApplicationTaskModel,
    CandidateProfileModel,
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
    SearchRegion,
    SystemState,
    TaskState,
    VacancyData,
    VacancyState,
    WorkFormat,
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
            session.add(
                CandidateProfileModel(
                    account_id=account.id,
                    active_resume_id=resume.id,
                    display_name="Тимур",
                )
            )
            directions = DirectionRepository(session)
            direction = directions.create(
                account.id,
                "ИТ",
                description="Другие подходящие технические роли",
                scoring_config={
                    "role_scope": "IT_ADJACENT",
                    "search_settings": {
                        "employment_forms": ["FULL"],
                        "minimum_salary": 120000,
                        "desired_salary": 180000,
                        "remote_all_russia": True,
                    },
                },
            )
            query = directions.add_query(
                direction.id,
                "Python разработчик",
                regions=(SearchRegion("3", "Екатеринбург"),),
                work_formats=(WorkFormat.REMOTE,),
                schedule_minutes=120,
            )

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
        assert dashboard_data["search_enabled"] is True
        assert dashboard_data["resource_saving_mode"] is True
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
        assert dashboard_data["background"] == {
            "state": "NOT_STARTED",
            "last_success_at": None,
            "next_search_at": None,
            "next_messages_at": None,
            "next_statuses_at": None,
            "error": None,
        }
        stored_direction = dashboard_data["directions"][0]
        assert stored_direction["name"] == "Другое ИТ"
        assert stored_direction["role_scope"] == "IT_ADJACENT"
        assert stored_direction["queued"] == 1
        assert stored_direction["rejected"] == 1
        assert stored_direction["queries"] == ["Python разработчик"]
        assert stored_direction["regions"] == [{"area": "3", "name": "Екатеринбург"}]
        assert stored_direction["work_formats"] == ["REMOTE"]
        assert stored_direction["employment_forms"] == ["FULL"]
        assert stored_direction["minimum_salary"] == 120000
        assert stored_direction["desired_salary"] == 180000
        assert stored_direction["remote_all_russia"] is True
        assert stored_direction["schedule_minutes"] == 120
        assert dashboard_data["incidents"][0]["code"] == "UI_TEST_WARNING"

        options = request(app, "GET", "/api/directions/options")
        assert options.status_code == 200
        assert {"area": "3", "name": "Екатеринбург"} in options.json()["regions"]

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
            "direction": "Другое ИТ",
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

        sent = request(app, "GET", f"/api/sent?account_id={account_id}")
        assert sent.status_code == 200
        assert sent.json()[0]["vacancy_id"] == "ui-303"
        assert sent.json()[0]["title"] == "Серверный разработчик"
        assert sent.json()[0]["resume_title"] == "Python"
        assert sent.json()[0]["direction"] == "Другое ИТ"
        assert sent.json()[0]["state"] == "APPLIED"
        assert sent.json()[0]["applied_at"]

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

        assert request(app, "POST", "/api/search/pause").status_code == 403
        search_paused = request(app, "POST", "/api/search/pause", headers=headers)
        assert search_paused.status_code == 200
        assert search_paused.json() == {
            "search_enabled": False,
            "resource_saving_mode": True,
        }
        search_resumed = request(app, "POST", "/api/search/resume", headers=headers)
        assert search_resumed.status_code == 200
        assert search_resumed.json() == {
            "search_enabled": True,
            "resource_saving_mode": True,
        }

        resource_path = "/api/background/resource-saving"
        assert request(app, "PUT", resource_path, json={"enabled": False}).status_code == 403
        resource_saving = request(
            app,
            "PUT",
            resource_path,
            headers=headers,
            json={"enabled": False},
        )
        assert resource_saving.status_code == 200
        assert resource_saving.json() == {
            "search_enabled": True,
            "resource_saving_mode": False,
        }
        assert (
            request(
                app,
                "PUT",
                resource_path,
                headers=headers,
                json={"enabled": "нет"},
            ).status_code
            == 422
        )
        background_dashboard = request(
            app,
            "GET",
            f"/api/dashboard?account_id={account_id}",
        ).json()
        assert background_dashboard["search_enabled"] is True
        assert background_dashboard["resource_saving_mode"] is False

        database = create_database(settings)
        try:
            with database.sessions.begin() as session:
                notification_settings = session.get(ApplicationSettingsModel, 1)
                assert notification_settings is not None
                notification_settings.notification_routing = {
                    "CAPTCHA_REQUIRED": ["WINDOWS"],
                }
        finally:
            database.close()

        communications_path = f"/api/communications?account_id={account_id}"
        communications = request(app, "GET", communications_path)
        assert communications.status_code == 200
        communications_data = communications.json()
        assert communications_data["unread_messages"] == 1
        assert communications_data["unseen_invitations"] == 1
        assert communications_data["conversations"][0]["vacancy_id"] == vacancy_id
        assert communications_data["conversations"][0]["messages"][0]["body"] == (
            "Приглашаем познакомиться"
        )
        assert set(communications_data["notification_settings"]["routing"]) == {
            "NEW_MESSAGE",
            "INVITATION",
            "REPLY_REQUIRED",
            "FORM_REQUIRED",
            "AUTH_REQUIRED",
            "ACCOUNT_WARNING",
            "UNKNOWN_RESULT",
            "CRITICAL_ERROR",
            "DAILY_SUMMARY",
            "CAPTCHA_REQUIRED",
        }

        read_path = (
            f"/api/communications/conversations/"
            f"{communications_data['conversations'][0]['application_id']}/read"
            f"?account_id={account_id}"
        )
        assert request(app, "POST", read_path).status_code == 403
        read = request(app, "POST", read_path, headers=headers)
        assert read.status_code == 200
        assert read.json()["unread_messages"] == 0

        draft_path = (
            f"/api/communications/conversations/"
            f"{communications_data['conversations'][0]['application_id']}/draft"
            f"?account_id={account_id}"
        )
        draft = request(
            app,
            "PUT",
            draft_path,
            headers=headers,
            json={"body": "Здравствуйте! Готов обсудить вакансию."},
        )
        assert draft.status_code == 200
        outgoing = next(
            message
            for message in draft.json()["conversations"][0]["messages"]
            if message["direction"] == "OUTGOING"
        )
        assert outgoing["state"] == "REVIEW_REQUIRED"
        assert outgoing["content_hash"]
        confirm_path = (
            f"/api/communications/messages/{outgoing['id']}/confirm?account_id={account_id}"
        )
        confirmed = request(
            app,
            "POST",
            confirm_path,
            headers=headers,
            json={
                "content_hash": outgoing["content_hash"],
                "content_version": outgoing["content_version"],
            },
        )
        assert confirmed.status_code == 200
        confirmed_outgoing = next(
            message
            for message in confirmed.json()["conversations"][0]["messages"]
            if message["id"] == outgoing["id"]
        )
        assert confirmed_outgoing["state"] == "CONFIRMED"
        assert all(
            message["state"] != "SENT"
            for message in confirmed.json()["conversations"][0]["messages"]
        )

        invitation_id = communications_data["invitations"][0]["id"]
        invitation_seen = request(
            app,
            "POST",
            (f"/api/communications/invitations/{invitation_id}/seen?account_id={account_id}"),
            headers=headers,
        )
        assert invitation_seen.status_code == 200
        assert invitation_seen.json()["unseen_invitations"] == 0

        notification_values = {
            "windows_enabled": False,
            "telegram_enabled": True,
            "email_enabled": False,
            "events": ["NEW_MESSAGE", "AUTH_REQUIRED"],
        }
        notification_path = f"/api/communications/notifications?account_id={account_id}"
        assert (
            request(
                app,
                "PUT",
                notification_path,
                json=notification_values,
            ).status_code
            == 403
        )
        notifications = request(
            app,
            "PUT",
            notification_path,
            headers=headers,
            json=notification_values,
        )
        assert notifications.status_code == 200
        saved_notifications = notifications.json()["notification_settings"]
        assert saved_notifications["windows_enabled"] is False
        assert saved_notifications["telegram_enabled"] is True
        assert saved_notifications["routing"]["NEW_MESSAGE"] == ["TELEGRAM"]
        assert saved_notifications["routing"]["AUTH_REQUIRED"] == ["TELEGRAM"]
        assert saved_notifications["routing"]["INVITATION"] == []
        assert "CAPTCHA_REQUIRED" not in saved_notifications["routing"]

        direction_values = {
            "is_active": False,
            "queries": ["Fullstack Python", "Интеграции Python API"],
            "regions": [
                {"area": "1", "name": "Москва"},
                {"area": "3", "name": "Екатеринбург"},
            ],
            "work_formats": ["REMOTE", "HYBRID"],
            "employment_forms": ["FULL", "PROJECT"],
            "minimum_salary": 150000,
            "desired_salary": 220000,
            "remote_all_russia": True,
            "schedule_minutes": 90,
        }
        direction_path = f"/api/directions/{stored_direction['id']}?account_id={account_id}"
        assert request(app, "PUT", direction_path, json=direction_values).status_code == 403
        saved_direction = request(
            app,
            "PUT",
            direction_path,
            headers=headers,
            json=direction_values,
        )
        assert saved_direction.status_code == 200
        assert saved_direction.json()["name"] == "Другое ИТ"
        assert saved_direction.json()["is_active"] is False
        assert saved_direction.json()["queries"] == direction_values["queries"]
        assert saved_direction.json()["regions"] == direction_values["regions"]
        assert saved_direction.json()["work_formats"] == ["REMOTE", "HYBRID"]
        assert saved_direction.json()["employment_forms"] == ["FULL", "PROJECT"]
        assert saved_direction.json()["minimum_salary"] == 150000
        assert saved_direction.json()["desired_salary"] == 220000
        assert saved_direction.json()["remote_all_russia"] is True
        assert saved_direction.json()["schedule_minutes"] == 90

        invalid_direction = request(
            app,
            "PUT",
            direction_path,
            headers=headers,
            json={**direction_values, "work_formats": [], "remote_all_russia": True},
        )
        assert invalid_direction.status_code == 422

        values = {
            "daily_limit": 51,
            "delay_min_seconds": 36,
            "delay_max_seconds": 72,
        }
        assert request(app, "PUT", "/api/queue/settings", json=values).status_code == 403
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
            "/api/sent?account_id=99999",
            "/api/communications?account_id=99999",
            f"/api/vacancies/missing?account_id={account_id}",
        ):
            response = request(app, "GET", path)
            assert response.status_code == 404
    finally:
        app.state.database.close()


def test_unknown_result_is_reconciled_only_by_explicit_choice(settings: Settings) -> None:
    account_id, vacancy_id, _rejected_id = seed_workspace(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            task_id = session.scalar(
                select(ApplicationTaskModel.id)
                .join(ApplicationModel)
                .where(ApplicationModel.account_id == account_id)
            )
            assert task_id is not None
            tasks = QueueTaskRepository(session)
            claimed = tasks.claim_next(datetime.now(UTC))
            assert claimed is not None
            tasks.transition(
                task_id,
                TaskState.UNKNOWN_RESULT,
                error_code="RESULT_NOT_CONFIRMED",
            )
            SystemStateRepository(session).transition(SystemState.PAUSED)
    finally:
        database.close()

    app = create_app(settings)
    try:
        queue = request(app, "GET", f"/api/queue?account_id={account_id}")
        unknown = next(item for item in queue.json() if item["vacancy_id"] == vacancy_id)
        assert unknown["state"] == "UNKNOWN_RESULT"

        path = f"/api/queue/{task_id}/reconcile?account_id={account_id}"
        assert (
            request(
                app,
                "POST",
                path,
                json={"status": "APPLIED"},
            ).status_code
            == 403
        )
        session_key = request(app, "GET", "/api/session").json()["key"]
        reconciled = request(
            app,
            "POST",
            path,
            headers={"X-Hugin-Session": session_key},
            json={"status": "APPLIED"},
        )
        assert reconciled.status_code == 200
        assert reconciled.json() == {
            "task_state": "COMPLETED",
            "application_state": "APPLIED",
            "blocking": False,
        }

        sent = request(app, "GET", f"/api/sent?account_id={account_id}")
        assert {item["vacancy_id"] for item in sent.json()} == {"ui-101", "ui-303"}
        resumed = request(
            app,
            "POST",
            "/api/queue/resume",
            headers={"X-Hugin-Session": session_key},
        )
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "RUNNING"

        repeated = request(
            app,
            "POST",
            path,
            headers={"X-Hugin-Session": session_key},
            json={"status": "APPLIED"},
        )
        assert repeated.status_code == 409
    finally:
        app.state.database.close()
