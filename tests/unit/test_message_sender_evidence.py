from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.adapters.hh_browser import CHAT_MESSAGES_SCRIPT, VisibleHhBrowser
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationModel, CoverLetterModel, IncidentModel
from hugin.domain.content import (
    CoverLetterState,
    IncidentState,
    MessageDirection,
    RecruiterActionKind,
    RecruiterActionSource,
    RecruiterActionState,
    RecruiterMessageState,
)
from hugin.domain.hh_sync import HhChatMessageData
from hugin.repositories.communications import CommunicationRepository
from hugin.services.autonomous_replies import AutonomousReplyService
from hugin.services.hh_sync import HhSynchronizationService
from hugin.services.ui_communications import UiCommunicationService
from tests.unit.test_communications import create_application

pytestmark = pytest.mark.integration

LETTER = (
    "Здравствуйте!\n\nВ проекте Пример разрабатываю сервис на FastAPI с PostgreSQL. "
    "Сам планирую задачи, проверяю изменения и принимаю архитектурные решения.\n\n"
    "Готов подробно рассказать, как организована серверная часть."
)
CHAT_HEADER = (
    '<a data-qa="chatik-header-vacancy-link" '
    'href="https://hh.ru/vacancy/101?hhtmFrom=chat">Перейти</a>'
)


def saved_letter(
    session: Session, state: CoverLetterState = CoverLetterState.SENT
) -> tuple[int, int]:
    account_id, application_id = create_application(
        session, account_label="Проверка автора", vacancy_hh_id="101"
    )
    application = session.get(ApplicationModel, application_id)
    assert application is not None
    session.add(
        CoverLetterModel(
            application_id=application.id,
            vacancy_id=application.vacancy_id,
            resume_id=application.resume_id,
            instruction_version="sender-check",
            model_name="local-check",
            context_hash="sender-check",
            state=state,
            text=LETTER,
        )
    )
    session.flush()
    return account_id, application_id


def parse_local_chat(body: str, *, own: bool = False) -> tuple[HhChatMessageData, ...]:
    marker = '<span data-qa="chat-bubble-icon-read"></span>' if own else ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(
                CHAT_HEADER + '<article data-qa="chatik-chat-message-501">'
                f'<p data-qa="chat-bubble-text">{escape(body)}</p>{marker}</article>'
            )
            payload = page.evaluate(CHAT_MESSAGES_SCRIPT, "101")
        finally:
            browser.close()
    reader = VisibleHhBrowser(Path("unused"), "unused", "unused", "unused", 500)
    return reader._parse_chat_messages(payload, "101")


def test_unread_letter_without_sender_marker_is_kept_for_review_not_for_auto_reply(
    settings: Settings,
) -> None:
    messages = parse_local_chat(LETTER)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = saved_letter(session)
            result = HhSynchronizationService(session).synchronize_messages_with_new_ids(
                account_id=account_id, messages=messages
            )
            assert result.new_incoming_message_ids == ()
            stored = CommunicationRepository(session).list_messages_for_account(account_id)
            assert len(stored) == 1 and stored[0].body == LETTER
            assert stored[0].direction is MessageDirection.INCOMING
            incident = session.scalar(select(IncidentModel))
            assert incident is not None and incident.code == "HH_MESSAGE_SENDER_UNCERTAIN"
            batch = AutonomousReplyService(session).prepare(
                account_id=account_id, incoming_message_ids=(stored[0].id,), include_backlog=True
            )
            assert batch.drafts_created == 0 and batch.approved == ()
            ui = UiCommunicationService(session).get(account_id)
            assert ui.conversations[0].application_id == application_id
            assert ui.conversations[0].messages[0].sender_review_required
            assert ui.conversations[0].messages[0].body == LETTER
    finally:
        database.close()


def test_an_employer_quotation_with_a_new_question_is_preserved(settings: Settings) -> None:
    body = f"Вы написали:\n{LETTER}\nРасскажите, какую задачу решали самостоятельно?"
    messages = parse_local_chat(body)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, _ = saved_letter(session)
            result = HhSynchronizationService(session).synchronize_messages_with_new_ids(
                account_id=account_id, messages=messages
            )
            stored = CommunicationRepository(session).list_messages_for_account(account_id)
            assert result.new_incoming_message_ids == (stored[0].id,)
            assert stored[0].body == body
            assert session.scalar(select(IncidentModel)) is None
    finally:
        database.close()


def test_a_positive_outgoing_marker_never_starts_a_reply(settings: Settings) -> None:
    messages = parse_local_chat(LETTER, own=True)
    assert messages[0].direction is MessageDirection.OUTGOING
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, _ = saved_letter(session)
            result = HhSynchronizationService(session).synchronize_messages_with_new_ids(
                account_id=account_id, messages=messages, checked_at=datetime.now(UTC)
            )
            assert result.new_incoming_message_ids == ()
            batch = AutonomousReplyService(session).prepare(
                account_id=account_id, include_backlog=True
            )
            assert batch.drafts_created == 0 and batch.approved == ()
            assert session.scalar(select(IncidentModel)) is None
    finally:
        database.close()


@pytest.mark.parametrize("marker", ["sent", "outgoing", "employer"])
def test_current_sender_markers_are_explicit_and_time_is_read(marker: str) -> None:
    classes = "magritte-outgoing___current" if marker == "outgoing" else ""
    icon = (
        '<span data-qa="chat-bubble-icon-sent" data-status="sent"></span>'
        if marker == "sent"
        else ""
    )
    avatar = '<span data-qa="chat-bubble-avatar"></span>' if marker == "employer" else ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                CHAT_HEADER + '<div data-qa="chatik-chat-message-501">'
                '<div data-qa="chatik-chat-message-bubble chatik-chat-message-501-text" '
                f'class="{classes}">'
                f'{avatar}<span data-qa="chat-bubble-text">{escape(LETTER)}</span>'
                f'<span data-qa="chat-bubble-meta">{icon}'
                '<span data-qa="chat-bubble-display-time">13:37</span></span></div></div>'
            )
            payload = page.evaluate(CHAT_MESSAGES_SCRIPT, "101")
        finally:
            browser.close()
    messages = VisibleHhBrowser(
        Path("unused"), "unused", "unused", "unused", 500
    )._parse_chat_messages(payload, "101")
    assert len(messages) == 1
    assert messages[0].body == LETTER
    assert messages[0].direction is (
        MessageDirection.INCOMING if marker == "employer" else MessageDirection.OUTGOING
    )
    assert not messages[0].direction_inferred
    assert messages[0].displayed_time == "13:37"


def test_sender_marker_inside_quoted_body_is_not_own_sender_evidence() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                CHAT_HEADER + '<div data-qa="chatik-chat-message-501">'
                '<span data-qa="chat-bubble-text">Цитата'
                '<span data-qa="chat-bubble-icon-read"></span></span></div>'
            )
            payload = page.evaluate(CHAT_MESSAGES_SCRIPT, "101")
        finally:
            browser.close()
    messages = VisibleHhBrowser(
        Path("unused"), "unused", "unused", "unused", 500
    )._parse_chat_messages(payload, "101")
    assert messages[0].direction is MessageDirection.INCOMING
    assert messages[0].direction_inferred


def test_conflicting_sender_markers_stop_chat_reading() -> None:
    from playwright.sync_api import Error

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                CHAT_HEADER + '<div data-qa="chatik-chat-message-501">'
                '<span data-qa="chat-bubble-text">Текст</span>'
                '<span data-qa="chat-bubble-avatar"></span>'
                '<span data-qa="chat-bubble-icon-sent"></span></div>'
            )
            with pytest.raises(Error, match="противоречивые признаки отправителя"):
                page.evaluate(CHAT_MESSAGES_SCRIPT, "101")
        finally:
            browser.close()


@pytest.mark.parametrize(
    "header",
    [
        "",
        CHAT_HEADER.replace("/101?", "/999?"),
        '<div data-qa="chatik-chat-message-500"><span data-qa="chat-bubble-text">'
        + CHAT_HEADER
        + "</span></div>",
    ],
)
def test_chat_messages_require_the_expected_vacancy_in_the_real_header(header: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(
                header + '<article data-qa="chatik-chat-message-501">'
                '<span data-qa="chat-bubble-text">Вопрос из прежнего чата</span></article>'
            )
            assert page.evaluate(CHAT_MESSAGES_SCRIPT, "101") is None
            assert (
                VisibleHhBrowser._outgoing_messages_snapshot(page.main_frame, "101", "Ответ")
                is None
            )
        finally:
            browser.close()


@pytest.mark.parametrize("direction", [MessageDirection.OUTGOING, MessageDirection.INCOMING])
def test_positive_sender_resolves_only_matching_review_and_survives_repeat(
    settings: Settings, direction: MessageDirection
) -> None:
    database = create_database(settings)
    observed_at = datetime(2026, 10, 30, 12, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = saved_letter(session)
            repo = CommunicationRepository(session)
            question = repo.save_incoming(
                application_id=application_id,
                hh_id="question",
                body="Какую зарплату рассматриваете?",
                received_at=observed_at,
            )
            repo.record_message_action(
                account_id=account_id,
                message_id=question.id,
                kind=RecruiterActionKind.REPLY,
                state=RecruiterActionState.REQUIRED,
                source=RecruiterActionSource.SYSTEM,
                reason_code="QUESTION",
                reason="Нужен ответ",
                due_at=None,
                changed_at=observed_at,
                preserve_resolved=False,
            )
            service = HhSynchronizationService(session)
            weak = HhChatMessageData(
                "101", "501", MessageDirection.INCOMING, LETTER, direction_inferred=True
            )
            service.synchronize_messages_with_new_ids(
                account_id=account_id, messages=(weak,), checked_at=observed_at
            )
            original = next(
                m for m in repo.list_messages_for_account(account_id) if m.hh_id == "501"
            )
            confirmed = HhChatMessageData("101", "501", direction, LETTER)
            resolved = service.synchronize_messages_with_new_ids(
                account_id=account_id, messages=(confirmed,)
            )
            incident = session.scalar(select(IncidentModel))
            assert incident is not None and incident.state is IncidentState.RESOLVED
            assert resolved.metrics["sender_reviews_resolved"] == 1
            assert resolved.metrics.get("message_conflicts", 0) == 0
            assert resolved.new_incoming_message_ids == (
                (original.id,) if direction is MessageDirection.INCOMING else ()
            )
            stored = repo.get_message(account_id, original.id)
            expected = asdict(original)
            expected["direction"] = direction
            expected["state"] = (
                RecruiterMessageState.SENT
                if direction is MessageDirection.OUTGOING
                else RecruiterMessageState.RECEIVED
            )
            assert asdict(stored) == expected
            assert (
                next(
                    a
                    for a in repo.list_message_actions_for_account(account_id)
                    if a.message_id == question.id
                ).state
                is RecruiterActionState.REQUIRED
            )
            again = service.synchronize_messages_with_new_ids(
                account_id=account_id, messages=(confirmed, weak)
            )
            assert again.metrics.get("sender_reviews_resolved", 0) == 0
            assert again.metrics.get("message_conflicts", 0) == 0
            assert again.new_incoming_message_ids == ()
            resolved_incident = session.scalar(select(IncidentModel))
            assert resolved_incident is not None
            assert resolved_incident.state is IncidentState.RESOLVED
            assert len(repo.list_messages_for_account(account_id)) == 2
    finally:
        database.close()


@pytest.mark.parametrize("change", ["body", "weak"])
def test_sender_review_stays_open_without_matching_positive_evidence(
    settings: Settings, change: str
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, _ = saved_letter(session)
            service = HhSynchronizationService(session)
            weak = HhChatMessageData(
                "101", "501", MessageDirection.INCOMING, LETTER, direction_inferred=True
            )
            service.synchronize_messages_with_new_ids(account_id=account_id, messages=(weak,))
            observed = (
                HhChatMessageData("101", "501", MessageDirection.OUTGOING, LETTER + "Изменение")
                if change == "body"
                else weak
            )
            result = service.synchronize_messages_with_new_ids(
                account_id=account_id, messages=(observed,)
            )
            assert result.metrics.get("sender_reviews_resolved", 0) == 0
            review = session.scalar(
                select(IncidentModel).where(IncidentModel.code == "HH_MESSAGE_SENDER_UNCERTAIN")
            )
            assert review is not None and review.state is IncidentState.OPEN
            stored = CommunicationRepository(session).list_messages_for_account(account_id)
            assert stored[0].direction is MessageDirection.INCOMING
            assert stored[0].body == LETTER
    finally:
        database.close()


def test_worker_records_sender_confirmation_evidence_after_resolution(settings: Settings) -> None:
    import json

    from hugin.domain.automation import AutomationJobKind
    from hugin.services.decision_evidence import fingerprint
    from hugin.workers.hh_sync import HhSyncJobHandler

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            saved_letter(session)
        handler = HhSyncJobHandler(settings, AutomationJobKind.MESSAGES)
        weak = HhChatMessageData(
            "101", "501", MessageDirection.INCOMING, LETTER, direction_inferred=True
        )
        strong = HhChatMessageData("101", "501", MessageDirection.OUTGOING, LETTER)
        handler._synchronize_messages((weak,), allow_replies=False)
        handler._synchronize_messages((strong,), allow_replies=False)
        evidence_id = fingerprint(asdict(strong))
        path = settings.data_dir / "evidence/models" / f"{evidence_id}-response.json"
        assert path.is_file()
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["payload"] == json.loads(json.dumps(asdict(strong)))
        assert document["source_sha256"]
        with database.sessions() as session:
            resolved_incident = session.scalar(select(IncidentModel))
            assert resolved_incident is not None
            assert resolved_incident.state is IncidentState.RESOLVED
    finally:
        database.close()


@pytest.mark.parametrize("origin", ["cover_letter", "sent_reply"])
def test_corrected_historical_copy_does_not_answer_or_hide_a_new_question(
    settings: Settings, origin: str
) -> None:
    from hugin.domain.applications import ApplicationState
    from hugin.services.autonomy import DEFAULT_AUTONOMY_POLICY, AutonomyPolicyService
    from hugin.services.background_processes import BackgroundProcessService
    from hugin.workers.replies import ReplyWorker

    database = create_database(settings)
    observed_at = datetime(2026, 10, 30, 12, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = saved_letter(session)
            application = session.get(ApplicationModel, application_id)
            assert application is not None
            application.state = ApplicationState.APPLIED
            AutonomyPolicyService(session).update(
                {
                    **DEFAULT_AUTONOMY_POLICY,
                    "auto_prepare_replies": True,
                    "reply_templates": [
                        {
                            "key": "interest",
                            "incoming_text": "Предложение ещё актуально?",
                            "response_text": "Здравствуйте! Да, готов обсудить.",
                            "enabled": True,
                        }
                    ],
                }
            )
            BackgroundProcessService(session, account_id).set_enabled("replies", True)
            repo = CommunicationRepository(session)
            old_body = LETTER if origin == "cover_letter" else "Добрый день! Интересно обсудить."
            if origin == "sent_reply":
                repo.save_synced_message(
                    application_id=application_id,
                    hh_id="old-sent",
                    direction=MessageDirection.OUTGOING,
                    body=old_body,
                    occurred_at=observed_at,
                )
            question = repo.save_incoming(
                application_id=application_id,
                hh_id="new-question",
                body="Предложение ещё актуально?",
                received_at=observed_at,
            )
            repo.record_message_action(
                account_id=account_id,
                message_id=question.id,
                kind=RecruiterActionKind.REPLY,
                state=RecruiterActionState.REQUIRED,
                source=RecruiterActionSource.SYSTEM,
                reason_code="QUESTION",
                reason="Нужен ответ",
                due_at=None,
                changed_at=observed_at,
                preserve_resolved=False,
            )
            service = HhSynchronizationService(session)
            weak = HhChatMessageData(
                "101", "late-copy", MessageDirection.INCOMING, old_body, direction_inferred=True
            )
            service.synchronize_messages_with_new_ids(
                account_id=account_id, messages=(weak,), checked_at=observed_at
            )
            strong = HhChatMessageData("101", "late-copy", MessageDirection.OUTGOING, old_body)
            for _ in range(2):
                service.synchronize_messages_with_new_ids(account_id=account_id, messages=(strong,))
                assert (
                    next(
                        a
                        for a in repo.list_message_actions_for_account(account_id)
                        if a.message_id == question.id
                    ).state
                    is RecruiterActionState.REQUIRED
                )
            # Переписка без явной записи действия также сохраняет видимый вопрос.
            from hugin.database.models import RecruiterMessageActionModel

            action = session.get(
                RecruiterMessageActionModel, (question.id, RecruiterActionKind.REPLY)
            )
            session.delete(action)
            session.flush()
            assert UiCommunicationService(session).get(account_id).conversations[0].needs_reply
            assert ReplyWorker(settings, account_id=account_id)._claim(session) == (
                application_id,
                question.id,
            )
            batch = AutonomousReplyService(session).prepare(
                account_id=account_id, incoming_message_ids=(question.id,), include_backlog=True
            )
            assert len(batch.approved) == 1
    finally:
        database.close()


def enable_sender_test_replies(session: Session, account_id: int) -> None:
    from hugin.services.autonomy import DEFAULT_AUTONOMY_POLICY, AutonomyPolicyService
    from hugin.services.background_processes import BackgroundProcessService

    AutonomyPolicyService(session).update(
        {
            **DEFAULT_AUTONOMY_POLICY,
            "reply_templates": [
                {
                    "key": "interest",
                    "incoming_text": "Предложение ещё актуально?",
                    "response_text": "Здравствуйте! Да, готов обсудить.",
                    "enabled": True,
                }
            ],
        }
    )
    BackgroundProcessService(session, account_id).set_enabled("replies", True)


def test_corrected_copy_keeps_an_existing_approved_reply_sendable(settings: Settings) -> None:
    from hugin.domain.applications import ApplicationState

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = saved_letter(session)
            application = session.get(ApplicationModel, application_id)
            assert application is not None
            application.state = ApplicationState.APPLIED
            enable_sender_test_replies(session, account_id)
            repo = CommunicationRepository(session)
            question = repo.save_incoming(
                application_id=application_id,
                hh_id="question",
                body="Предложение ещё актуально?",
                received_at=datetime.now(UTC),
            )
            replies = AutonomousReplyService(session)
            first = replies.prepare(account_id=account_id, incoming_message_ids=(question.id,))
            assert len(first.approved) == 1
            approved = first.approved[0]
            sync = HhSynchronizationService(session)
            sync.synchronize_messages_with_new_ids(
                account_id=account_id,
                messages=(
                    HhChatMessageData(
                        "101",
                        "late-copy",
                        MessageDirection.INCOMING,
                        LETTER,
                        direction_inferred=True,
                    ),
                ),
            )
            sync.synchronize_messages_with_new_ids(
                account_id=account_id,
                messages=(
                    HhChatMessageData("101", "late-copy", MessageDirection.OUTGOING, LETTER),
                ),
            )
            assert (
                replies.prepare(
                    account_id=account_id, incoming_message_ids=(question.id,), include_backlog=True
                ).approved
                == first.approved
            )
            assert replies.approved_for_send(
                account_id=account_id,
                message_id=approved.message_id,
                content_version=approved.content_version,
                content_hash=approved.content_hash,
            )
    finally:
        database.close()


def test_a_new_conflict_on_a_corrected_copy_still_blocks_preparation(settings: Settings) -> None:
    from hugin.domain.applications import ApplicationState

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = saved_letter(session)
            application = session.get(ApplicationModel, application_id)
            assert application is not None
            application.state = ApplicationState.APPLIED
            enable_sender_test_replies(session, account_id)
            repo = CommunicationRepository(session)
            question = repo.save_incoming(
                application_id=application_id,
                hh_id="question",
                body="Предложение ещё актуально?",
                received_at=datetime.now(UTC),
            )
            sync = HhSynchronizationService(session)
            sync.synchronize_messages_with_new_ids(
                account_id=account_id,
                messages=(
                    HhChatMessageData(
                        "101",
                        "late-copy",
                        MessageDirection.INCOMING,
                        LETTER,
                        direction_inferred=True,
                    ),
                ),
            )
            sync.synchronize_messages_with_new_ids(
                account_id=account_id,
                messages=(
                    HhChatMessageData("101", "late-copy", MessageDirection.OUTGOING, LETTER),
                ),
            )
            conflict = sync.synchronize_messages_with_new_ids(
                account_id=account_id,
                messages=(
                    HhChatMessageData("101", "late-copy", MessageDirection.INCOMING, LETTER),
                ),
            )
            assert conflict.metrics["message_conflicts"] == 1
            batch = AutonomousReplyService(session).prepare(
                account_id=account_id, incoming_message_ids=(question.id,), include_backlog=True
            )
            assert batch.approved == () and batch.drafts_created == 0
            assert batch.skipped_manual == 1
    finally:
        database.close()
