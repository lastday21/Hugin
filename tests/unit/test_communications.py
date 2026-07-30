# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import (
    check_database_schema,
    create_database,
    current_revision,
    downgrade_database,
    upgrade_database,
)
from hugin.domain.communications import (
    CommunicationStateError,
    MessageSendOutcome,
    StaleMessageDraftError,
)
from hugin.domain.content import (
    InvitationState,
    NotificationChannel,
    RecruiterMessageState,
)
from hugin.domain.vacancies import VacancyData
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.services.communications import CommunicationService, RecordingMessageSender

pytestmark = pytest.mark.integration


def create_application(
    session: Session,
    *,
    account_label: str,
    vacancy_hh_id: str,
) -> tuple[int, int]:
    account = AccountRepository(session).create(account_label)
    resume = ResumeRepository(session).upsert(
        account.id,
        f"resume-{vacancy_hh_id}",
        f"Резюме {account_label}",
    )
    vacancy = VacancyRepository(session).upsert(
        VacancyData(
            hh_id=vacancy_hh_id,
            title=f"Вакансия {vacancy_hh_id}",
            source_url=f"https://hh.ru/vacancy/{vacancy_hh_id}",
        )
    )
    application = ApplicationRepository(session).create_apply_intent(
        account.id,
        vacancy.id,
        resume.id,
    )
    return account.id, application.id


def test_communications_migration_preserves_existing_rows(settings: Settings) -> None:
    upgrade_database(settings, "0012_automation_jobs")
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            _, application_id = create_application(
                session,
                account_label="Миграция",
                vacancy_hh_id="communications-migration",
            )

        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO recruiter_messages "
                    "(application_id, hh_id, direction, body, state, received_at, created_at) "
                    "VALUES (:application_id, 'legacy-message', 'INCOMING', "
                    "'Здравствуйте', 'RECEIVED', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"application_id": application_id},
            )
            connection.execute(
                text(
                    "INSERT INTO invitations "
                    "(application_id, hh_id, title, state, created_at, updated_at) "
                    "VALUES (:application_id, 'legacy-invitation', 'Собеседование', "
                    "'RECEIVED', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"application_id": application_id},
            )
            connection.execute(
                text(
                    "INSERT INTO notifications "
                    "(application_id, event_type, channel, state, payload, "
                    "scheduled_at, created_at) "
                    "VALUES (:application_id, 'NEW_MESSAGE', 'WINDOWS', 'PENDING', "
                    "'{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"application_id": application_id},
            )
    finally:
        database.close()

    upgrade_database(settings)
    assert current_revision(settings) == "0016_safe_application_defaults"
    check_database_schema(settings)

    migrated = create_database(settings)
    try:
        columns = {
            column["name"] for column in inspect(migrated.engine).get_columns("recruiter_messages")
        }
        assert {"read_at", "content_hash", "version"} <= columns
        assert "seen_at" in {
            column["name"] for column in inspect(migrated.engine).get_columns("invitations")
        }
        assert "deduplication_key" in {
            column["name"] for column in inspect(migrated.engine).get_columns("notifications")
        }
        assert "ai_prompt_overrides" in {
            column["name"]
            for column in inspect(migrated.engine).get_columns("application_settings")
        }

        with migrated.engine.begin() as connection:
            message = connection.execute(
                text(
                    "SELECT read_at, content_hash, version "
                    "FROM recruiter_messages WHERE hh_id = 'legacy-message'"
                )
            ).one()
            notification_key = connection.execute(
                text("SELECT deduplication_key FROM notifications")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO recruiter_messages "
                    "(application_id, direction, body, state, version, created_at) "
                    "VALUES (:application_id, 'OUTGOING', 'Неясная отправка', "
                    "'UNKNOWN_RESULT', 1, CURRENT_TIMESTAMP)"
                ),
                {"application_id": application_id},
            )

        assert message == (None, None, 1)
        assert notification_key.startswith("legacy:")
    finally:
        migrated.close()

    downgrade_database(settings, "0012_automation_jobs")
    downgraded = create_database(settings)
    try:
        message_columns = {
            column["name"]
            for column in inspect(downgraded.engine).get_columns("recruiter_messages")
        }
        assert {"read_at", "content_hash", "version"}.isdisjoint(message_columns)
        with downgraded.engine.connect() as connection:
            states = connection.execute(
                text("SELECT state FROM recruiter_messages ORDER BY id")
            ).scalars()
            assert tuple(states) == ("RECEIVED", "FAILED")
    finally:
        downgraded.close()


def test_incoming_invitations_and_notifications_are_idempotent(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    received_at = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    read_at = received_at + timedelta(minutes=1)

    try:
        with database.sessions.begin() as session:
            first_account_id, first_application_id = create_application(
                session,
                account_label="Первый",
                vacancy_hh_id="communications-first",
            )
            second_account_id, second_application_id = create_application(
                session,
                account_label="Второй",
                vacancy_hh_id="communications-second",
            )
            service = CommunicationService(session, RecordingMessageSender())

            incoming = service.save_incoming(
                application_id=first_application_id,
                hh_id="incoming-1",
                body="Приглашаем обсудить вакансию",
                received_at=received_at,
            )
            repeated = service.save_incoming(
                application_id=first_application_id,
                hh_id="incoming-1",
                body="Приглашаем обсудить вакансию",
                received_at=received_at,
            )
            service.save_incoming(
                application_id=second_application_id,
                hh_id="incoming-2",
                body="Сообщение второго аккаунта",
                received_at=received_at,
            )

            assert repeated.id == incoming.id
            assert service.messages(first_account_id) == (incoming,)
            assert len(service.messages(second_account_id)) == 1

            marked = service.mark_incoming_read(
                account_id=first_account_id,
                message_id=incoming.id,
                read_at=read_at,
            )
            marked_again = service.mark_incoming_read(
                account_id=first_account_id,
                message_id=incoming.id,
                read_at=read_at + timedelta(minutes=1),
            )
            assert marked.read_at == read_at
            assert marked_again.read_at == read_at

            invitation = service.save_invitation(
                application_id=first_application_id,
                hh_id="invitation-1",
                title="Первичное собеседование",
                details="Предлагаем созвониться",
                updated_at=received_at,
            )
            updated_invitation = service.save_invitation(
                application_id=first_application_id,
                hh_id="invitation-1",
                title="Собеседование с командой",
                details="Предлагаем созвониться завтра",
                updated_at=read_at,
            )
            assert updated_invitation.id == invitation.id
            assert updated_invitation.title == "Собеседование с командой"
            assert len(service.invitations(first_account_id)) == 1
            seen = service.mark_invitation_seen(
                account_id=first_account_id,
                invitation_id=invitation.id,
                seen_at=read_at,
            )
            assert seen.seen_at == read_at
            assert seen.state is InvitationState.RECEIVED

            notification = service.enqueue_notification(
                deduplication_key="message:incoming-1:windows",
                event_type="new_message",
                channel=NotificationChannel.WINDOWS,
                payload={"title": "Новое сообщение"},
                application_id=first_application_id,
                scheduled_at=received_at,
            )
            repeated_notification = service.enqueue_notification(
                deduplication_key="message:incoming-1:windows",
                event_type="new_message",
                channel=NotificationChannel.WINDOWS,
                payload={"title": "Не должно заменить первое событие"},
                application_id=first_application_id,
                scheduled_at=read_at,
            )
            assert repeated_notification.id == notification.id
            assert repeated_notification.payload == {"title": "Новое сообщение"}
    finally:
        database.close()


def test_outgoing_draft_requires_exact_confirmation_and_sends_once(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Черновик",
                vacancy_hh_id="communications-draft",
            )
            sender = RecordingMessageSender()
            service = CommunicationService(session, sender)
            draft = service.create_outgoing_draft(
                application_id=application_id,
                body="Здравствуйте! Готов обсудить задачи.",
            )

            assert draft.state is RecruiterMessageState.REVIEW_REQUIRED
            assert draft.content_version == 1
            assert draft.content_hash == service.content_hash(draft.body)

            with pytest.raises(StaleMessageDraftError):
                service.confirm_outgoing_draft(
                    account_id=account_id,
                    message_id=draft.id,
                    content_version=draft.content_version + 1,
                    content_hash=draft.content_hash,
                    confirmed_at=now,
                )

            confirmed = service.confirm_outgoing_draft(
                account_id=account_id,
                message_id=draft.id,
                content_version=draft.content_version,
                content_hash=draft.content_hash,
                confirmed_at=now,
            )
            assert confirmed.state is RecruiterMessageState.CONFIRMED

            edited = service.edit_outgoing_draft(
                account_id=account_id,
                message_id=draft.id,
                body="Здравствуйте! Готов обсудить задачи команды.",
            )
            assert edited.content_version == 2
            assert edited.state is RecruiterMessageState.REVIEW_REQUIRED
            assert edited.confirmed_at is None
            assert edited.content_hash is not None

            sent = service.confirm_and_send(
                account_id=account_id,
                message_id=edited.id,
                content_version=edited.content_version,
                content_hash=edited.content_hash,
                now=now + timedelta(minutes=1),
            )
            repeated = service.confirm_and_send(
                account_id=account_id,
                message_id=edited.id,
                content_version=edited.content_version,
                content_hash=edited.content_hash,
                now=now + timedelta(minutes=2),
            )

            assert sent.state is RecruiterMessageState.SENT
            assert repeated.state is RecruiterMessageState.SENT
            assert len(sender.attempts) == 1
            assert sender.attempts[0].body == edited.body
            with pytest.raises(CommunicationStateError):
                service.edit_outgoing_draft(
                    account_id=account_id,
                    message_id=edited.id,
                    body="Повторная правка отправленного сообщения",
                )
    finally:
        database.close()


def test_unknown_send_result_is_never_repeated(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Неизвестный результат",
                vacancy_hh_id="communications-unknown",
            )
            sender = RecordingMessageSender(MessageSendOutcome.UNKNOWN_RESULT)
            service = CommunicationService(session, sender)
            draft = service.create_outgoing_draft(
                application_id=application_id,
                body="Подтверждённый ответ",
            )
            assert draft.content_hash is not None

            unknown = service.confirm_and_send(
                account_id=account_id,
                message_id=draft.id,
                content_version=draft.content_version,
                content_hash=draft.content_hash,
                now=now,
            )
            repeated = service.confirm_and_send(
                account_id=account_id,
                message_id=draft.id,
                content_version=draft.content_version,
                content_hash=draft.content_hash,
                now=now + timedelta(minutes=1),
            )

            assert unknown.state is RecruiterMessageState.UNKNOWN_RESULT
            assert repeated.state is RecruiterMessageState.UNKNOWN_RESULT
            assert len(sender.attempts) == 1
            with pytest.raises(CommunicationStateError):
                service.edit_outgoing_draft(
                    account_id=account_id,
                    message_id=draft.id,
                    body="Нельзя менять до сверки результата",
                )
    finally:
        database.close()
