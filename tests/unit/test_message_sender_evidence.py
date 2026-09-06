from __future__ import annotations

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
from hugin.domain.content import CoverLetterState, MessageDirection
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
                '<article data-qa="chatik-chat-message-501">'
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
