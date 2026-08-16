# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Never

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
    MessageDirection,
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
from hugin.repositories.communications import CommunicationRepository
from hugin.services.autonomous_replies import AutonomousReplyService
from hugin.services.autonomy import DEFAULT_AUTONOMY_POLICY, AutonomyPolicyService
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


@pytest.mark.empty_database
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
            application = connection.execute(
                text(
                    "SELECT vacancy_id, resume_id, direction_id "
                    "FROM applications WHERE id = :application_id"
                ),
                {"application_id": application_id},
            ).one()
            connection.execute(
                text(
                    "INSERT INTO cover_letters "
                    "(application_id, vacancy_id, direction_id, resume_id, text, "
                    "instruction_version, model_name, state) "
                    "VALUES (:application_id, :vacancy_id, :direction_id, :resume_id, "
                    "'Старое письмо', 'legacy-instruction', 'legacy-model', 'SENT')"
                ),
                {
                    "application_id": application_id,
                    "vacancy_id": application.vacancy_id,
                    "direction_id": application.direction_id,
                    "resume_id": application.resume_id,
                },
            )
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
    assert current_revision(settings) == "0025_screening_availability"
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
        assert "notification_cutoffs" in {
            column["name"]
            for column in inspect(migrated.engine).get_columns("application_settings")
        }
        assert {
            "generation_mode",
            "router_model_name",
            "router_confidence",
            "router_reason",
        } <= {column["name"] for column in inspect(migrated.engine).get_columns("cover_letters")}

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
            generation_mode = connection.execute(
                text(
                    "SELECT generation_mode FROM cover_letters "
                    "WHERE instruction_version = 'legacy-instruction'"
                )
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
        assert generation_mode == "LEGACY"
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
                body="Подтверждённый ответ \n",
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

            reconciled, created = CommunicationRepository(session).save_synced_message(
                application_id=application_id,
                hh_id="hh-confirmed-outgoing",
                direction=MessageDirection.OUTGOING,
                body=draft.body.strip(),
                occurred_at=now + timedelta(minutes=2),
            )

            assert created is False
            assert reconciled.id == unknown.id
            assert reconciled.state is RecruiterMessageState.SENT
            assert reconciled.hh_id == "hh-confirmed-outgoing"
    finally:
        database.close()


def test_unknown_outgoing_reconciliation_ignores_known_history(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Старое исходящее",
                vacancy_hh_id="communications-known-outgoing",
            )
            repository = CommunicationRepository(session)
            old_message, old_created = repository.save_synced_message(
                application_id=application_id,
                hh_id="old-outgoing",
                direction=MessageDirection.OUTGOING,
                body="Одинаковый ответ",
                occurred_at=now - timedelta(days=1),
            )
            assert old_created

            service = CommunicationService(
                session,
                RecordingMessageSender(MessageSendOutcome.UNKNOWN_RESULT),
            )
            draft = service.create_outgoing_draft(
                application_id=application_id,
                body="Одинаковый ответ",
            )
            assert draft.content_hash is not None
            unknown = service.confirm_and_send(
                account_id=account_id,
                message_id=draft.id,
                content_version=draft.content_version,
                content_hash=draft.content_hash,
                now=now,
            )
            assert unknown.state is RecruiterMessageState.UNKNOWN_RESULT

            known_again, created_again = repository.save_synced_message(
                application_id=application_id,
                hh_id="old-outgoing",
                direction=MessageDirection.OUTGOING,
                body="Одинаковый ответ",
                occurred_at=now + timedelta(minutes=5),
            )
            still_unknown = repository.get_message(account_id, unknown.id)

            assert not created_again
            assert known_again.id == old_message.id
            assert still_unknown.state is RecruiterMessageState.UNKNOWN_RESULT
            assert still_unknown.hh_id is None

            reconciled, reconciled_created = repository.save_synced_message(
                application_id=application_id,
                hh_id="new-outgoing",
                direction=MessageDirection.OUTGOING,
                body="Одинаковый ответ",
                occurred_at=now + timedelta(minutes=6),
            )

            assert not reconciled_created
            assert reconciled.id == unknown.id
            assert reconciled.state is RecruiterMessageState.SENT
            assert reconciled.hh_id == "new-outgoing"
    finally:
        database.close()


@pytest.mark.parametrize(
    ("body", "observed_at"),
    (
        ("Другой ответ", datetime(2026, 7, 26, 10, 5, tzinfo=UTC)),
        ("Точный ответ", datetime(2026, 7, 26, 10, 31, tzinfo=UTC)),
    ),
)
def test_unknown_outgoing_reconciliation_requires_exact_recent_message(
    settings: Settings,
    body: str,
    observed_at: datetime,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label=f"Сверка {body}",
                vacancy_hh_id=f"communications-reconcile-{observed_at.minute}-{len(body)}",
            )
            service = CommunicationService(
                session,
                RecordingMessageSender(MessageSendOutcome.UNKNOWN_RESULT),
            )
            draft = service.create_outgoing_draft(
                application_id=application_id,
                body="Точный ответ",
            )
            assert draft.content_hash is not None
            unknown = service.confirm_and_send(
                account_id=account_id,
                message_id=draft.id,
                content_version=draft.content_version,
                content_hash=draft.content_hash,
                now=now,
            )

            synchronized, created = CommunicationRepository(session).save_synced_message(
                application_id=application_id,
                hh_id=f"unmatched-{observed_at.minute}",
                direction=MessageDirection.OUTGOING,
                body=body,
                occurred_at=observed_at,
            )
            stored_unknown = CommunicationRepository(session).get_message(
                account_id,
                unknown.id,
            )

            assert created
            assert synchronized.id != unknown.id
            assert stored_unknown.state is RecruiterMessageState.UNKNOWN_RESULT
            assert stored_unknown.hh_id is None
    finally:
        database.close()


def test_exact_approved_safe_reply_is_prepared_for_automatic_send(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Утверждённый ответ",
                vacancy_hh_id="communications-approved-reply",
            )
            incoming = CommunicationService(
                session,
                RecordingMessageSender(),
            ).save_incoming(
                application_id=application_id,
                hh_id="incoming-approved",
                body="Предложение ещё актуально?",
            )
            AutonomyPolicyService(session).update(
                {
                    **DEFAULT_AUTONOMY_POLICY,
                    "reply_templates": [
                        {
                            "key": "interest",
                            "incoming_text": "Предложение ещё актуально?",
                            "response_text": "Здравствуйте! Да, готов обсудить детали.",
                            "enabled": True,
                        }
                    ],
                }
            )

            old_message_batch = AutonomousReplyService(session).prepare(
                account_id=account_id,
            )
            assert old_message_batch.approved == ()

            batch = AutonomousReplyService(session).prepare(
                account_id=account_id,
                incoming_message_ids=(incoming.id,),
            )

            assert len(batch.approved) == 1
            outgoing = next(
                message
                for message in CommunicationService(
                    session,
                    RecordingMessageSender(),
                ).messages(account_id)
                if message.direction is MessageDirection.OUTGOING
            )
            assert outgoing.state is RecruiterMessageState.CONFIRMED
            assert outgoing.auto_send_approved is True
            assert outgoing.reply_template_key == "interest"
            assert AutonomousReplyService(session).approved_for_send(
                account_id=account_id,
                message_id=outgoing.id,
                content_version=outgoing.content_version,
                content_hash=outgoing.content_hash or "",
            )

            pending_retry = AutonomousReplyService(session).prepare(account_id=account_id)
            assert len(pending_retry.approved) == 1
            assert pending_retry.approved[0].message_id == outgoing.id
            assert (
                len(
                    tuple(
                        message
                        for message in CommunicationService(
                            session,
                            RecordingMessageSender(),
                        ).messages(account_id)
                        if message.direction is MessageDirection.OUTGOING
                    )
                )
                == 1
            )

            failed = CommunicationService(
                session,
                RecordingMessageSender(MessageSendOutcome.FAILED),
            ).send_confirmed(
                account_id=account_id,
                message_id=outgoing.id,
                content_version=outgoing.content_version,
                content_hash=outgoing.content_hash or "",
            )
            assert failed.state is RecruiterMessageState.FAILED

            retry = AutonomousReplyService(session).prepare(account_id=account_id)
            assert len(retry.approved) == 1
            assert retry.approved[0].message_id == outgoing.id
            assert (
                CommunicationRepository(session).get_message(account_id, outgoing.id).state
                is RecruiterMessageState.CONFIRMED
            )
            current_policy = AutonomyPolicyService(session).get()
            AutonomyPolicyService(session).update(
                {
                    **current_policy.as_payload(),
                    "auto_send_approved_replies": False,
                }
            )
            assert not AutonomousReplyService(session).approved_for_send(
                account_id=account_id,
                message_id=outgoing.id,
                content_version=outgoing.content_version,
                content_hash=outgoing.content_hash or "",
            )
            AutonomyPolicyService(session).update(current_policy.as_payload())

            unknown = CommunicationService(
                session,
                RecordingMessageSender(MessageSendOutcome.UNKNOWN_RESULT),
            ).send_confirmed(
                account_id=account_id,
                message_id=outgoing.id,
                content_version=outgoing.content_version,
                content_hash=outgoing.content_hash or "",
            )
            assert unknown.state is RecruiterMessageState.UNKNOWN_RESULT
            assert AutonomousReplyService(session).prepare(account_id=account_id).approved == ()
            with pytest.raises(CommunicationStateError, match="Повтор разрешён"):
                CommunicationService(
                    session,
                    RecordingMessageSender(),
                ).confirm_outgoing_retry(
                    account_id=account_id,
                    message_id=outgoing.id,
                    content_version=outgoing.content_version,
                    content_hash=outgoing.content_hash or "",
                )
    finally:
        database.close()


@pytest.mark.parametrize(
    ("suffix", "incoming_body", "response_body"),
    (
        (
            "incoming",
            "Какая зарплата вас устроит?",
            "Предлагаю обсудить условия в переписке.",
        ),
        (
            "response",
            "Предложение ещё актуально?",
            "Рассматриваю предложения от 150 000 рублей.",
        ),
        (
            "time",
            "Предложение ещё актуально?",
            "Да, давайте созвонимся завтра в 15:00.",
        ),
        (
            "url",
            "Предложение ещё актуально?",
            "Да, подробности здесь: https://example.com/profile",
        ),
        (
            "english",
            "Would you like to schedule an interview?",
            "Yes, I am available.",
        ),
        (
            "tomorrow",
            "Давайте завтра?",
            "Да, завтра удобно.",
        ),
        (
            "weekday",
            "Подойдёт понедельник?",
            "Да, подойдёт.",
        ),
        (
            "english-tomorrow",
            "Are you free tomorrow?",
            "Yes.",
        ),
        (
            "currency-prefix",
            "Предложение ещё актуально?",
            "Ожидаю $2500.",
        ),
        (
            "zoom",
            "Can you join Zoom?",
            "Yes.",
        ),
        (
            "conversation",
            "Хотели бы пообщаться о вакансии?",
            "Да, предложение интересно.",
        ),
        (
            "slot",
            "Выберите удобный слот в календаре.",
            "Хорошо.",
        ),
        (
            "english-calendar",
            "Please book a slot in my calendar.",
            "Sure.",
        ),
    ),
)
def test_approved_reply_with_risky_content_stays_manual(
    settings: Settings,
    suffix: str,
    incoming_body: str,
    response_body: str,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label=f"Зарплата вручную {suffix}",
                vacancy_hh_id=f"communications-salary-reply-{suffix}",
            )
            incoming = CommunicationService(
                session,
                RecordingMessageSender(),
            ).save_incoming(
                application_id=application_id,
                hh_id=f"incoming-salary-{suffix}",
                body=incoming_body,
            )
            AutonomyPolicyService(session).update(
                {
                    **DEFAULT_AUTONOMY_POLICY,
                    "reply_templates": [
                        {
                            "key": "salary",
                            "incoming_text": incoming_body,
                            "response_text": response_body,
                            "enabled": True,
                        }
                    ],
                }
            )

            batch = AutonomousReplyService(session).prepare(
                account_id=account_id,
                incoming_message_ids=(incoming.id,),
            )

            assert batch.approved == ()
            assert batch.skipped_manual == 1
    finally:
        database.close()


@pytest.mark.parametrize(
    ("incoming_body", "expected_manual"),
    (
        (
            "К сожалению, сейчас мы не готовы пригласить вас на следующий этап.",
            0,
        ),
        ("Какие у вас зарплатные ожидания?", 1),
    ),
)
def test_automatic_reply_filter_does_not_construct_model_for_skipped_messages(
    settings: Settings,
    incoming_body: str,
    expected_manual: int,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    factory_calls = 0

    def model_factory() -> Never:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("Модель не должна вызываться")

    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Фильтр ответов",
                vacancy_hh_id=f"reply-filter-{expected_manual}",
            )
            incoming = CommunicationService(
                session,
                RecordingMessageSender(),
            ).save_incoming(
                application_id=application_id,
                hh_id=f"incoming-filter-{expected_manual}",
                body=incoming_body,
            )

            batch = AutonomousReplyService(session).prepare(
                account_id=account_id,
                model_factory=model_factory,
                incoming_message_ids=(incoming.id,),
            )

            assert batch.approved == ()
            assert batch.drafts_created == 0
            assert batch.skipped_manual == expected_manual
            assert batch.failed == 0
            assert factory_calls == 0
    finally:
        database.close()
