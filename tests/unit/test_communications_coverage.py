# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
from hugin.domain.content import (
    MessageDirection,
    NotificationChannel,
    RecruiterActionKind,
    RecruiterActionSource,
    RecruiterActionState,
    RecruiterMessageState,
)
from hugin.repositories.communications import CommunicationRepository
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


def test_ui_ignores_old_draft_after_closing_incoming_message(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Завершённый диалог",
                vacancy_hh_id="communications-ui-closing-message",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-earlier-question",
                body="Расскажите, пожалуйста, как вы применяли Celery?",
            )
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-closing-message",
                body="Спасибо за то, что ответили на вопрос",
            )
            communications.create_outgoing_draft(
                application_id=application_id,
                body="Старый черновик ответа",
            )

            conversation = UiCommunicationService(session).get(account_id).conversations[0]
            closing_message = next(
                message
                for message in conversation.messages
                if message.body == "Спасибо за то, что ответили на вопрос"
            )

            assert closing_message.direction == MessageDirection.INCOMING
            assert conversation.messages[-1].state == RecruiterMessageState.REVIEW_REQUIRED
            assert conversation.needs_reply is False
            assert conversation.unread_count == 0
            assert UiCommunicationService(session).get(account_id).unread_messages == 0
    finally:
        database.close()


@pytest.mark.parametrize(
    ("second_outcome", "expected_needs_reply"),
    (
        (MessageSendOutcome.SENT, False),
        (MessageSendOutcome.FAILED, True),
    ),
)
def test_ui_closes_repeated_question_only_after_two_sent_answers(
    settings: Settings,
    second_outcome: MessageSendOutcome,
    expected_needs_reply: bool,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label=f"Цикл бота {second_outcome.value}",
                vacancy_hh_id=f"communications-ui-repeat-{second_outcome.value.lower()}",
            )
            question = "На каком курсе ты учишься?"
            for index, (incoming_body, outcome) in enumerate(
                (
                    (question, MessageSendOutcome.SENT),
                    ("  НА КАКОМ КУРСЕ ТЫ УЧИШЬСЯ?  ", second_outcome),
                ),
                start=1,
            ):
                communications = CommunicationService(
                    session,
                    RecordingMessageSender(outcome),
                )
                communications.save_incoming(
                    application_id=application_id,
                    hh_id=f"incoming-ui-repeat-{index}",
                    body=incoming_body,
                )
                draft = communications.create_outgoing_draft(
                    application_id=application_id,
                    body="Учусь на последнем курсе.",
                )
                communications.confirm_and_send(
                    account_id=account_id,
                    message_id=draft.id,
                    content_version=draft.content_version,
                    content_hash=draft.content_hash or "",
                )
            CommunicationService(session, RecordingMessageSender()).save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-repeat-3",
                body=question,
            )

            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert conversation.needs_reply is expected_needs_reply
    finally:
        database.close()


@pytest.mark.parametrize(
    "closing_message",
    (
        "Тимур Фанисович, спасибо, что уделили нам время. Успехов!",
        "Мы продолжаем рассматривать кандидатов и вернёмся к вам с обратной связью.",
        "Мы продолжаем рассмотрение кандидатов, поэтому нам потребуется ещё немного времени.",
        "Ваше резюме передано руководителю. Мы сообщим о результате отбора.",
    ),
)
def test_ui_ignores_old_draft_after_other_closing_messages(
    settings: Settings,
    closing_message: str,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Другой завершённый диалог",
                vacancy_hh_id=f"communications-ui-closing-{abs(hash(closing_message))}",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-other-closing-message",
                body=closing_message,
            )
            communications.create_outgoing_draft(
                application_id=application_id,
                body="Лишний старый черновик",
            )

            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert conversation.needs_reply is False
    finally:
        database.close()


@pytest.mark.parametrize(
    "pending_state",
    (RecruiterMessageState.REVIEW_REQUIRED, RecruiterMessageState.FAILED),
)
def test_ui_keeps_actionable_question_with_pending_response(
    settings: Settings,
    pending_state: RecruiterMessageState,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label=f"Вопрос {pending_state.value}",
                vacancy_hh_id=f"communications-ui-question-{pending_state.value.lower()}",
            )
            communications = CommunicationService(
                session,
                RecordingMessageSender(MessageSendOutcome.FAILED),
            )
            communications.save_incoming(
                application_id=application_id,
                hh_id=f"incoming-ui-question-{pending_state.value.lower()}",
                body="Расскажите, пожалуйста, как вы применяли Celery?",
            )
            draft = communications.create_outgoing_draft(
                application_id=application_id,
                body="Использовал Celery для фоновой обработки задач.",
            )
            if pending_state is RecruiterMessageState.FAILED:
                communications.confirm_and_send(
                    account_id=account_id,
                    message_id=draft.id,
                    content_version=draft.content_version,
                    content_hash=draft.content_hash or "",
                )

            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert conversation.messages[-1].state == pending_state.value
            assert conversation.needs_reply is True
    finally:
        database.close()


def test_ui_keeps_pending_response_after_hh_invitation_reminder(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Напоминание о вопросе",
                vacancy_hh_id="communications-ui-question-reminder",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-question-before-reminder",
                body="Расскажите, пожалуйста, работали ли вы с RAG?",
            )
            communications.create_outgoing_draft(
                application_id=application_id,
                body="Прямого опыта с RAG пока нет.",
            )
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-question-reminder",
                body="Напоминаем: ответьте на приглашение работодателя.",
            )

            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert conversation.messages[-1].direction == MessageDirection.INCOMING
            assert conversation.needs_reply is True
    finally:
        database.close()


@pytest.mark.parametrize(
    ("resolution", "expected_needs_reply"),
    (
        (None, True),
        ("sent_before_reminder", False),
        ("sent_after_reminder", False),
        ("closing", False),
    ),
)
def test_ui_keeps_only_unresolved_questionnaire_after_hh_invitation_reminder(
    settings: Settings,
    resolution: str | None,
    expected_needs_reply: bool,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label=f"Анкета перед напоминанием {resolution}",
                vacancy_hh_id=f"ui-questionnaire-reminder-{resolution}",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            cover_letter = communications.create_outgoing_draft(
                application_id=application_id,
                body="Здравствуйте! Направляю отклик на вакансию.",
            )
            communications.confirm_and_send(
                account_id=account_id,
                message_id=cover_letter.id,
                content_version=cover_letter.content_version,
                content_hash=cover_letter.content_hash or "",
            )
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-questionnaire",
                body=("Предлагаем заполнить короткую анкету-знакомство: https://forms.gle/example"),
            )

            if resolution == "sent_before_reminder":
                confirmation = communications.create_outgoing_draft(
                    application_id=application_id,
                    body="Анкету заполнил.",
                )
                communications.confirm_and_send(
                    account_id=account_id,
                    message_id=confirmation.id,
                    content_version=confirmation.content_version,
                    content_hash=confirmation.content_hash or "",
                )
            elif resolution == "closing":
                communications.save_incoming(
                    application_id=application_id,
                    hh_id="incoming-ui-questionnaire-completed",
                    body="Спасибо за прохождение анкеты, ответы получены.",
                )

            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-hh-invitation-reminder",
                body=(
                    "Ответьте на приглашение, даже если оно вам не интересно. "
                    "Так мы сможем рекомендовать вам более подходящие вакансии. "
                    "Отправить ответ можно одной кнопкой:"
                ),
            )
            if resolution == "sent_after_reminder":
                confirmation = communications.create_outgoing_draft(
                    application_id=application_id,
                    body="Анкету заполнил.",
                )
                communications.confirm_and_send(
                    account_id=account_id,
                    message_id=confirmation.id,
                    content_version=confirmation.content_version,
                    content_hash=confirmation.content_hash or "",
                )

            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert conversation.messages[-1].direction == (
                MessageDirection.OUTGOING
                if resolution == "sent_after_reminder"
                else MessageDirection.INCOMING
            )
            assert conversation.needs_reply is expected_needs_reply
    finally:
        database.close()


def test_completed_external_form_stays_closed_after_hh_invitation_reminder(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Завершённая внешняя анкета",
                vacancy_hh_id="ui-completed-external-form",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            questionnaire = communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-completed-questionnaire",
                body=("Предлагаем заполнить короткую анкету: https://forms.gle/example"),
            )
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-completed-reminder",
                body="Напоминаем: ответьте на приглашение работодателя.",
            )
            before = UiCommunicationService(session).get(account_id).conversations[0]
            original = CommunicationRepository(session).get_message(
                account_id,
                questionnaire.id,
            )
            application = session.get(ApplicationModel, application_id)
            assert application is not None
            application_state = application.state
            outgoing_before = tuple(
                message
                for message in communications.messages(account_id)
                if message.direction is MessageDirection.OUTGOING
            )

            action = communications.complete_external_action(
                account_id=account_id,
                message_id=questionnaire.id,
                kind=RecruiterActionKind.EXTERNAL_FORM,
                reason_code="USER_CONFIRMED_SUBMITTED",
                reason="Пользователь подтвердил отправку внешней анкеты.",
            )
            after = UiCommunicationService(session).get(account_id).conversations[0]
            stored = CommunicationRepository(session).get_message(
                account_id,
                questionnaire.id,
            )
            outgoing_after = tuple(
                message
                for message in communications.messages(account_id)
                if message.direction is MessageDirection.OUTGOING
            )

            assert before.needs_reply is True
            assert after.needs_reply is False
            assert action.kind is RecruiterActionKind.EXTERNAL_FORM
            assert action.state is RecruiterActionState.COMPLETED
            assert action.source is RecruiterActionSource.USER
            assert action.reason_code == "USER_CONFIRMED_SUBMITTED"
            assert action.resolved_at is not None
            assert stored.direction is original.direction is MessageDirection.INCOMING
            assert stored.state is original.state is RecruiterMessageState.RECEIVED
            assert stored.read_at is original.read_at is None
            stored_application = session.get(ApplicationModel, application_id)
            assert stored_application is not None
            assert stored_application.state is application_state
            assert outgoing_after == outgoing_before == ()
    finally:
        database.close()


def test_action_recorded_on_hh_reminder_does_not_hide_unfinished_form(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Анкета и неверная отметка напоминания",
                vacancy_hh_id="ui-form-action-on-reminder",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-unfinished-questionnaire",
                body="Заполните анкету: https://forms.gle/example",
            )
            reminder = communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-marked-reminder",
                body="Напоминаем: ответьте на приглашение работодателя.",
            )
            communications.complete_external_action(
                account_id=account_id,
                message_id=reminder.id,
                kind=RecruiterActionKind.EXTERNAL_FORM,
                reason_code="WRONG_TARGET_TEST",
                reason="Проверка привязки действия к исходному сообщению.",
            )

            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert conversation.needs_reply is True
    finally:
        database.close()


def test_expired_external_action_is_dismissed_with_persisted_reason(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Просроченное внешнее действие",
                vacancy_hh_id="ui-expired-external-action",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            questionnaire = communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-expired-action",
                body="Заполните анкету: https://forms.gle/example",
            )
            communications.require_external_action(
                account_id=account_id,
                message_id=questionnaire.id,
                kind=RecruiterActionKind.EXTERNAL_FORM,
                source=RecruiterActionSource.RULE,
                reason_code="FORM_REQUIRED",
                reason="Работодатель запросил внешнюю анкету.",
                due_at=now - timedelta(minutes=1),
                changed_at=now - timedelta(days=1),
            )

            assert (
                communications.dismiss_expired_actions(
                    account_id=account_id,
                    changed_at=now,
                )
                == 1
            )
            action = communications.message_actions(account_id)[0]
            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert conversation.needs_reply is False
            assert action.state is RecruiterActionState.DISMISSED
            assert action.source is RecruiterActionSource.SYSTEM
            assert action.reason_code == "ACTION_EXPIRED"
            assert action.due_at == now - timedelta(minutes=1)
            assert action.resolved_at == now
            assert (now - timedelta(minutes=1)).isoformat() in action.reason
    finally:
        database.close()


def test_communications_get_persists_expired_action_resolution(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Просроченное действие через интерфейс",
                vacancy_hh_id="ui-expired-action-get",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            questionnaire = communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-expired-action-get",
                body="Заполните анкету: https://forms.gle/example",
            )
            communications.require_external_action(
                account_id=account_id,
                message_id=questionnaire.id,
                kind=RecruiterActionKind.EXTERNAL_FORM,
                source=RecruiterActionSource.RULE,
                reason_code="FORM_REQUIRED",
                reason="Работодатель запросил внешнюю анкету.",
                due_at=datetime.now(UTC) - timedelta(minutes=1),
            )
    finally:
        database.close()

    app = create_app(settings)
    try:
        response = request(app, "GET", f"/api/communications?account_id={account_id}")
        assert response.status_code == 200
        assert response.json()["conversations"][0]["needs_reply"] is False
    finally:
        app.state.database.close()

    database = create_database(settings)
    try:
        with database.sessions() as session:
            action = CommunicationService(
                session,
                RecordingMessageSender(),
            ).message_actions(account_id)[0]
            assert action.state is RecruiterActionState.DISMISSED
            assert action.reason_code == "ACTION_EXPIRED"
            assert action.resolved_at is not None
    finally:
        database.close()


def test_system_rule_does_not_reopen_completed_external_action(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Завершение сильнее фонового правила",
                vacancy_hh_id="ui-completed-action-protected",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            questionnaire = communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-protected-completion",
                body="Заполните анкету: https://forms.gle/example",
            )
            completed = communications.complete_external_action(
                account_id=account_id,
                message_id=questionnaire.id,
                kind=RecruiterActionKind.EXTERNAL_FORM,
                reason_code="USER_CONFIRMED_SUBMITTED",
                reason="Пользователь подтвердил отправку анкеты.",
            )
            repeated_rule = communications.require_external_action(
                account_id=account_id,
                message_id=questionnaire.id,
                kind=RecruiterActionKind.EXTERNAL_FORM,
                source=RecruiterActionSource.SYSTEM,
                reason_code="FORM_REQUIRED",
                reason="Повторная системная проверка формы.",
            )

            assert repeated_rule == completed
            assert repeated_rule.state is RecruiterActionState.COMPLETED
            assert repeated_rule.reason_code == "USER_CONFIRMED_SUBMITTED"
            conversation = UiCommunicationService(session).get(account_id).conversations[0]
            assert conversation.needs_reply is False
    finally:
        database.close()


@pytest.mark.parametrize(
    ("body", "required", "expected_needs_reply"),
    (
        ("Давайте пока оставим это здесь.", False, False),
        ("Давайте пока оставим это здесь.", True, True),
        ("Есть ли у вас опыт с Python?", False, True),
    ),
)
def test_ui_uses_model_decision_only_for_ambiguous_message(
    settings: Settings,
    body: str,
    required: bool,
    expected_needs_reply: bool,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label=f"Решение модели {required}",
                vacancy_hh_id=f"ui-model-decision-{required}-{abs(hash(body))}",
            )
            communications = CommunicationService(session, RecordingMessageSender())
            incoming = communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-ui-model-decision",
                body=body,
            )
            communications.record_reply_requirement(
                account_id=account_id,
                message_id=incoming.id,
                required=required,
                source=RecruiterActionSource.MODEL,
                reason_code=("MODEL_REPLY_REQUIRED" if required else "MODEL_NO_REPLY_REQUIRED"),
                reason="Сохранённое решение проверки неоднозначного сообщения.",
            )
            communications.mark_incoming_read(
                account_id=account_id,
                message_id=incoming.id,
            )

            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert conversation.needs_reply is expected_needs_reply
            assert conversation.unread_count == (1 if expected_needs_reply else 0)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("outcome", "expected_state", "expected_needs_reply"),
    (
        (MessageSendOutcome.SENT, RecruiterActionState.COMPLETED, False),
        (MessageSendOutcome.FAILED, RecruiterActionState.REQUIRED, True),
    ),
)
def test_reply_action_completes_only_after_confirmed_sent_outgoing(
    settings: Settings,
    outcome: MessageSendOutcome,
    expected_state: RecruiterActionState,
    expected_needs_reply: bool,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    sent_at = datetime(2026, 8, 29, 14, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label=f"Завершение ответа {outcome.value}",
                vacancy_hh_id=f"ui-reply-action-{outcome.value.lower()}",
            )
            communications = CommunicationService(
                session,
                RecordingMessageSender(outcome),
            )
            incoming = communications.save_incoming(
                application_id=application_id,
                hh_id=f"incoming-ui-reply-action-{outcome.value.lower()}",
                body="Давайте пока оставим это здесь.",
            )
            communications.record_reply_requirement(
                account_id=account_id,
                message_id=incoming.id,
                required=True,
                source=RecruiterActionSource.MODEL,
                reason_code="MODEL_REPLY_REQUIRED",
                reason="Неоднозначное сообщение требует ответа.",
            )
            draft = communications.create_outgoing_draft(
                application_id=application_id,
                body="Понял, спасибо за сообщение.",
            )
            communications.confirm_and_send(
                account_id=account_id,
                message_id=draft.id,
                content_version=draft.content_version,
                content_hash=draft.content_hash or "",
                now=sent_at,
            )

            action = communications.message_actions(account_id)[0]
            stored_incoming = CommunicationRepository(session).get_message(
                account_id,
                incoming.id,
            )
            conversation = UiCommunicationService(session).get(account_id).conversations[0]

            assert action.state is expected_state
            assert conversation.needs_reply is expected_needs_reply
            assert stored_incoming.read_at is None
            if outcome is MessageSendOutcome.SENT:
                assert action.source is RecruiterActionSource.SYSTEM
                assert action.reason_code == "REPLY_SENT"
                assert action.resolved_at == sent_at
            else:
                assert action.source is RecruiterActionSource.MODEL
                assert action.resolved_at is None
    finally:
        database.close()
