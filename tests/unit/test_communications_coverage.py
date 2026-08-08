# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import ApplicationModel, ApplicationSettingsModel
from hugin.domain.applications import ApplicationState
from hugin.domain.communications import (
    CommunicationNotFoundError,
    CommunicationStateError,
    MessageSendOutcome,
    MessageSendRequest,
)
from hugin.domain.content import NotificationChannel, RecruiterMessageState
from hugin.services.ai_prompts import ALICE_AI_MODEL, QWEN3_AI_MODEL
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.ui_communications import UiCommunicationService
from tests.unit.test_communications import create_application
from tests.unit.test_workspace_api import request, seed_workspace

pytestmark = pytest.mark.integration


def test_communications_api_edits_draft_and_rejects_foreign_data(
    settings: Settings,
) -> None:
    account_id, _vacancy_id, _rejected_id = seed_workspace(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            application_id = session.scalar(
                select(ApplicationModel.id).where(ApplicationModel.account_id == account_id)
            )
            assert application_id is not None
            foreign_account_id, _foreign_application_id = create_application(
                session,
                account_label="Другой аккаунт",
                vacancy_hh_id="communications-api-foreign",
            )
    finally:
        database.close()

    app = create_app(settings)
    try:
        session_key = request(app, "GET", "/api/session").json()["key"]
        headers = {"X-Hugin-Session": session_key}
        draft_path = (
            f"/api/communications/conversations/{application_id}/draft?account_id={account_id}"
        )

        assert (
            request(
                app,
                "PUT",
                draft_path,
                json={"body": "Первый вариант ответа"},
            ).status_code
            == 403
        )
        invalid_body = request(
            app,
            "PUT",
            draft_path,
            headers=headers,
            json={"body": ""},
        )
        assert invalid_body.status_code == 422

        created = request(
            app,
            "PUT",
            draft_path,
            headers=headers,
            json={"body": "Первый вариант ответа"},
        )
        assert created.status_code == 200
        first = next(
            message
            for message in created.json()["conversations"][0]["messages"]
            if message["direction"] == "OUTGOING"
        )

        edited = request(
            app,
            "PUT",
            draft_path,
            headers=headers,
            json={"body": "Исправленный вариант ответа"},
        )
        assert edited.status_code == 200
        second = next(
            message
            for message in edited.json()["conversations"][0]["messages"]
            if message["direction"] == "OUTGOING"
        )
        assert second["id"] == first["id"]
        assert second["body"] == "Исправленный вариант ответа"
        assert second["content_version"] == first["content_version"] + 1

        stale_confirmation = request(
            app,
            "POST",
            (f"/api/communications/messages/{second['id']}/confirm?account_id={account_id}"),
            headers=headers,
            json={
                "content_hash": first["content_hash"],
                "content_version": first["content_version"],
            },
        )
        assert stale_confirmation.status_code == 409
        malformed_confirmation = request(
            app,
            "POST",
            (f"/api/communications/messages/{second['id']}/confirm?account_id={account_id}"),
            headers=headers,
            json={"content_hash": "bad", "content_version": 1},
        )
        assert malformed_confirmation.status_code == 422

        foreign_query = f"?account_id={foreign_account_id}"
        assert (
            request(
                app,
                "PUT",
                f"/api/communications/conversations/{application_id}/draft{foreign_query}",
                headers=headers,
                json={"body": "Чужой ответ"},
            ).status_code
            == 404
        )
        assert (
            request(
                app,
                "POST",
                f"/api/communications/conversations/{application_id}/read{foreign_query}",
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            request(
                app,
                "POST",
                f"/api/communications/messages/{second['id']}/confirm{foreign_query}",
                headers=headers,
                json={
                    "content_hash": second["content_hash"],
                    "content_version": second["content_version"],
                },
            ).status_code
            == 404
        )

        communications = request(
            app,
            "GET",
            f"/api/communications?account_id={account_id}",
        ).json()
        invitation_id = communications["invitations"][0]["id"]
        assert (
            request(
                app,
                "POST",
                (f"/api/communications/invitations/{invitation_id}/seen{foreign_query}"),
                headers=headers,
            ).status_code
            == 404
        )

        invalid_event = request(
            app,
            "PUT",
            f"/api/communications/notifications?account_id={account_id}",
            headers=headers,
            json={
                "windows_enabled": True,
                "telegram_enabled": False,
                "email_enabled": False,
                "events": ["NEW_MESSAGE", "SOMETHING_ELSE"],
            },
        )
        assert invalid_event.status_code == 422
        assert request(app, "GET", "/api/communications?account_id=0").status_code == 422
    finally:
        app.state.database.close()


def test_communications_api_handles_no_messages_and_unknown_result(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Пустой диалог",
                vacancy_hh_id="communications-api-empty",
            )
    finally:
        database.close()

    app = create_app(settings)
    try:
        session_key = request(app, "GET", "/api/session").json()["key"]
        headers = {"X-Hugin-Session": session_key}
        communications_path = f"/api/communications?account_id={account_id}"
        empty = request(app, "GET", communications_path)
        assert empty.status_code == 200
        assert empty.json()["conversations"] == []
        assert empty.json()["invitations"] == []
        assert empty.json()["unread_messages"] == 0
        assert empty.json()["unseen_invitations"] == 0

        read = request(
            app,
            "POST",
            (f"/api/communications/conversations/{application_id}/read?account_id={account_id}"),
            headers=headers,
        )
        assert read.status_code == 200
        assert read.json()["conversations"] == []
    finally:
        app.state.database.close()

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = CommunicationService(
                session,
                RecordingMessageSender(MessageSendOutcome.UNKNOWN_RESULT),
            )
            draft = service.create_outgoing_draft(
                application_id=application_id,
                body="Ответ с неизвестным результатом",
            )
            assert draft.content_hash is not None
            unknown = service.confirm_and_send(
                account_id=account_id,
                message_id=draft.id,
                content_version=draft.content_version,
                content_hash=draft.content_hash,
                now=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
            )
            assert unknown.state is RecruiterMessageState.UNKNOWN_RESULT
    finally:
        database.close()

    app = create_app(settings)
    try:
        session_key = request(app, "GET", "/api/session").json()["key"]
        blocked = request(
            app,
            "PUT",
            (f"/api/communications/conversations/{application_id}/draft?account_id={account_id}"),
            headers={"X-Hugin-Session": session_key},
            json={"body": "Повторять отправку нельзя"},
        )
        assert blocked.status_code == 409
    finally:
        app.state.database.close()


def test_ai_prompt_settings_api_updates_and_resets(settings: Settings) -> None:
    account_id, _vacancy_id, _rejected_id = seed_workspace(settings)
    app = create_app(settings)
    path = f"/api/communications/ai-prompts?account_id={account_id}"
    try:
        session_key = request(app, "GET", "/api/session").json()["key"]
        headers = {"X-Hugin-Session": session_key}
        communications = request(
            app,
            "GET",
            f"/api/communications?account_id={account_id}",
        ).json()
        current = communications["ai_prompt_settings"]
        assert current["resume"] == current["defaults"]["resume"]
        models = communications["ai_model_settings"]
        assert models["selected"] == ALICE_AI_MODEL
        assert models["reasoning_effort"] == "high"
        assert [option["value"] for option in models["options"]] == [
            ALICE_AI_MODEL,
            QWEN3_AI_MODEL,
        ]
        assert [option["value"] for option in models["reasoning_options"]] == [
            "low",
            "medium",
            "high",
        ]
        model_path = f"/api/communications/ai-model?account_id={account_id}"
        assert (
            request(
                app,
                "PUT",
                model_path,
                json={"model": QWEN3_AI_MODEL, "reasoning_effort": "high"},
            ).status_code
            == 403
        )
        assert (
            request(
                app,
                "PUT",
                model_path,
                headers=headers,
                json={"model": "unknown/latest", "reasoning_effort": "high"},
            ).status_code
            == 422
        )
        selected = request(
            app,
            "PUT",
            model_path,
            headers=headers,
            json={"model": QWEN3_AI_MODEL, "reasoning_effort": "medium"},
        )
        assert selected.status_code == 200
        assert selected.json()["ai_model_settings"]["selected"] == QWEN3_AI_MODEL
        assert selected.json()["ai_model_settings"]["reasoning_effort"] == "medium"
        assert (
            request(
                app,
                "PUT",
                model_path,
                headers=headers,
                json={"model": QWEN3_AI_MODEL, "reasoning_effort": "unknown"},
            ).status_code
            == 422
        )
        assert request(app, "PUT", path, json=current["defaults"]).status_code == 403
        assert (
            request(
                app,
                "PUT",
                path,
                headers=headers,
                json={
                    "resume": "",
                    "cover_letter": "Пиши кратко.",
                    "recruiter_reply": "Отвечай по существу.",
                },
            ).status_code
            == 422
        )

        saved = request(
            app,
            "PUT",
            path,
            headers=headers,
            json={
                "resume": "Подчёркивай результат работы.",
                "cover_letter": "Пиши коротко и естественно.",
                "recruiter_reply": "Отвечай дружелюбно.",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["ai_prompt_settings"]["recruiter_reply"] == "Отвечай дружелюбно."

        reset = request(
            app,
            "POST",
            f"/api/communications/ai-prompts/reset?account_id={account_id}",
            headers=headers,
        )
        assert reset.status_code == 200
        prompts = reset.json()["ai_prompt_settings"]
        assert prompts["resume"] == prompts["defaults"]["resume"]
        assert reset.json()["ai_model_settings"]["selected"] == QWEN3_AI_MODEL
        assert reset.json()["ai_model_settings"]["reasoning_effort"] == "medium"
        assert (
            request(
                app,
                "POST",
                "/api/communications/ai-prompts/reset?account_id=99999",
                headers=headers,
            ).status_code
            == 404
        )
    finally:
        app.state.database.close()


def test_communication_services_validate_inputs_without_sending(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Проверка данных",
                vacancy_hh_id="communications-validation",
            )
            sender = RecordingMessageSender()
            service = CommunicationService(session, sender)

            with pytest.raises(ValueError):
                service.messages(0)
            with pytest.raises(ValueError):
                service.create_outgoing_draft(application_id=application_id, body=" ")
            with pytest.raises(ValueError):
                service.save_incoming(
                    application_id=application_id,
                    hh_id=" ",
                    body="Сообщение",
                )
            with pytest.raises(ValueError):
                service.save_incoming(
                    application_id=application_id,
                    hh_id="incoming-invalid-body",
                    body=" ",
                )
            with pytest.raises(ValueError):
                service.save_invitation(
                    application_id=application_id,
                    hh_id="invitation-without-title",
                    title=" ",
                )
            with pytest.raises(ValueError):
                service.enqueue_notification(
                    deduplication_key=" ",
                    event_type="NEW_MESSAGE",
                    channel=NotificationChannel.WINDOWS,
                    payload={},
                )
            with pytest.raises(ValueError):
                service.enqueue_notification(
                    deduplication_key="validation-event",
                    event_type=" ",
                    channel=NotificationChannel.WINDOWS,
                    payload={},
                )
            with pytest.raises(ValueError):
                service.confirm_outgoing_draft(
                    account_id=account_id,
                    message_id=1,
                    content_version=1,
                    content_hash="not-a-hash",
                )

            draft = service.create_outgoing_draft(
                application_id=application_id,
                body="Черновик без подтверждения",
            )
            assert draft.content_hash is not None
            with pytest.raises(CommunicationStateError):
                service.send_confirmed(
                    account_id=account_id,
                    message_id=draft.id,
                    content_version=draft.content_version,
                    content_hash=draft.content_hash,
                )
            with pytest.raises(CommunicationNotFoundError):
                service.edit_outgoing_draft(
                    account_id=account_id + 999,
                    message_id=draft.id,
                    body="Чужая правка",
                )

            direct_request = MessageSendRequest(
                message_id=draft.id,
                application_id=application_id,
                body=draft.body,
                content_hash=draft.content_hash,
                content_version=draft.content_version,
            )
            first = sender.send(direct_request)
            repeated = sender.send(direct_request)
            assert repeated is first
            assert len(sender.attempts) == 1
    finally:
        database.close()


def test_ui_communications_reports_missing_settings_and_account(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, _application_id = create_application(
                session,
                account_label="Без настроек",
                vacancy_hh_id="communications-ui-missing",
            )
            service = UiCommunicationService(session)
            with pytest.raises(LookupError):
                service.update_notification_settings(
                    account_id=account_id + 999,
                    windows_enabled=True,
                    telegram_enabled=False,
                    email_enabled=False,
                    events=(),
                )

            session.execute(delete(ApplicationSettingsModel))
            session.flush()
            with pytest.raises(LookupError):
                service.get(account_id)
            with pytest.raises(LookupError):
                service.update_notification_settings(
                    account_id=account_id,
                    windows_enabled=True,
                    telegram_enabled=False,
                    email_enabled=False,
                    events=(),
                )
    finally:
        database.close()


def test_ui_marks_only_actionable_incoming_message_as_needing_reply(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Признак ответа",
                vacancy_hh_id="communications-ui-reply-filter",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-refusal",
                body="К сожалению, сейчас мы не готовы пригласить вас дальше.",
            )
            service = UiCommunicationService(session)
            assert service.get(account_id).conversations[0].needs_reply is False

            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-question",
                body="Расскажите, пожалуйста, как вы применяли Celery.",
            )
            assert service.get(account_id).conversations[0].needs_reply is True

            application = session.get(ApplicationModel, application_id)
            assert application is not None
            application.state = ApplicationState.REJECTED
            session.flush()
            assert service.get(account_id).conversations[0].needs_reply is False
    finally:
        database.close()
