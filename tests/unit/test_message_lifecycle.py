from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationModel,
    IncidentModel,
    InvitationModel,
    NotificationModel,
    RecruiterMessageModel,
)
from hugin.domain.communications import CommunicationStateError
from hugin.domain.content import (
    DeliveryState,
    IncidentSeverity,
    IncidentState,
    InvitationState,
    MessageDirection,
    RecruiterActionKind,
    RecruiterActionSource,
    RecruiterActionState,
    RecruiterMessageState,
)
from hugin.domain.hh_sync import HhChatMessageData
from hugin.domain.vacancies import VacancyData
from hugin.repositories import ApplicationRepository, VacancyRepository
from hugin.repositories.communications import CommunicationRepository
from hugin.services.application_statistics import application_statistics
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.hh_sync import HhSynchronizationService
from hugin.services.incidents import IncidentService
from hugin.services.notifications import NotificationService
from hugin.services.ui_communications import UiCommunicationService
from hugin.services.ui_workspace import UiWorkspaceService
from hugin.workers.replies import ReplyWorker
from tests.unit.test_message_sender_evidence import LETTER, saved_letter


def test_sender_correction_cancels_only_untouched_generated_reply(settings: Settings) -> None:
    db = create_database(settings)
    try:
        with db.sessions.begin() as session:
            account_id, app_id = saved_letter(session)
            sync = HhSynchronizationService(session)
            sync.synchronize_messages(
                account_id=account_id,
                messages=(
                    HhChatMessageData(
                        "101", "501", MessageDirection.INCOMING, LETTER, direction_inferred=True
                    ),
                ),
            )
            source = session.scalar(select(RecruiterMessageModel))
            assert source is not None
            service = CommunicationService(session, RecordingMessageSender())
            generated = service.create_outgoing_draft(
                application_id=app_id,
                body="Автоматический ответ на своё письмо",
                reply_to_message_id=source.id,
            )
            edited = service.create_outgoing_draft(
                application_id=app_id,
                body="Сгенерированный текст",
                reply_to_message_id=source.id,
            )
            service.edit_outgoing_draft(
                account_id=account_id, message_id=edited.id, body="Мой сохранённый текст"
            )
            manual = service.create_outgoing_draft(application_id=app_id, body="Мой черновик")
            before = session.get(RecruiterMessageModel, generated.id)
            assert before is not None
            unchanged = (before.body, before.content_hash, before.version, before.created_at)
            positive = HhChatMessageData(
                "101", "501", MessageDirection.OUTGOING, LETTER, direction_inferred=False
            )

            sync.synchronize_messages(account_id=account_id, messages=(positive,))

            session.refresh(before)
            assert before.state is RecruiterMessageState.CANCELLED
            assert (
                before.body,
                before.content_hash,
                before.version,
                before.created_at,
            ) == unchanged
            edited_row = session.get(RecruiterMessageModel, edited.id)
            manual_row = session.get(RecruiterMessageModel, manual.id)
            assert (
                edited_row is not None and edited_row.state is RecruiterMessageState.REVIEW_REQUIRED
            )
            assert manual_row is not None and manual_row.body == "Мой черновик"
            sync.synchronize_messages(account_id=account_id, messages=(positive,))
            assert before.state is RecruiterMessageState.CANCELLED
    finally:
        db.close()


def test_retired_copy_does_not_receive_completion_of_the_real_question(settings: Settings) -> None:
    db = create_database(settings)
    try:
        with db.sessions.begin() as session:
            account_id, app_id = saved_letter(session)
            repo = CommunicationRepository(session)
            incoming = repo.save_incoming(
                application_id=app_id, hh_id="real", body="Вопрос", received_at=datetime.now(UTC)
            )
            repo.record_message_action(
                account_id=account_id,
                message_id=incoming.id,
                kind=RecruiterActionKind.REPLY,
                state=RecruiterActionState.REQUIRED,
                source=RecruiterActionSource.SYSTEM,
                reason_code="QUESTION",
                reason="Требует ответа",
                due_at=None,
                changed_at=datetime.now(UTC),
                preserve_resolved=False,
            )
            copy = repo.save_incoming(
                application_id=app_id,
                hh_id="copy",
                body="Чужой вопрос",
                received_at=datetime.now(UTC),
            )
            copy_row = session.get(RecruiterMessageModel, copy.id)
            assert copy_row is not None
            copy_row.state = RecruiterMessageState.CANCELLED
            session.flush()
            sent, _ = repo.save_synced_message(
                application_id=app_id,
                hh_id="reply",
                direction=MessageDirection.OUTGOING,
                body="Ответ",
                occurred_at=datetime.now(UTC),
            )
            action = repo.complete_reply_action_for_sent_outgoing(
                account_id=account_id, message_id=sent.id, completed_at=datetime.now(UTC)
            )
            assert action is not None and action.message_id == incoming.id
            assert action.state is RecruiterActionState.COMPLETED
    finally:
        db.close()


def test_cancelled_automatic_reply_does_not_hide_a_later_question(settings: Settings) -> None:
    db = create_database(settings)
    try:
        with db.sessions.begin() as session:
            account_id, app_id = saved_letter(session)
            sync = HhSynchronizationService(session)
            sync.synchronize_messages(
                account_id=account_id,
                messages=(
                    HhChatMessageData(
                        "101", "501", MessageDirection.INCOMING, LETTER, direction_inferred=True
                    ),
                ),
            )
            source = session.scalar(select(RecruiterMessageModel))
            assert source is not None
            CommunicationService(session, RecordingMessageSender()).create_outgoing_draft(
                application_id=app_id,
                body="Неактуальный автоответ",
                reply_to_message_id=source.id,
            )
            sync.synchronize_messages(
                account_id=account_id,
                messages=(
                    HhChatMessageData(
                        "101", "501", MessageDirection.OUTGOING, LETTER, direction_inferred=False
                    ),
                ),
            )
            assert not UiCommunicationService(session).get(account_id).conversations[0].needs_reply
            CommunicationRepository(session).save_incoming(
                application_id=app_id,
                hh_id="502",
                body="Когда сможете выйти?",
                received_at=datetime.now(UTC),
            )
            assert UiCommunicationService(session).get(account_id).conversations[0].needs_reply
    finally:
        db.close()


def test_verified_duplicate_chat_import_is_retired_with_a_link_and_full_history(
    settings: Settings,
) -> None:
    db = create_database(settings)
    try:
        with db.sessions.begin() as session:
            account_id, app_id = saved_letter(session)
            app = session.get(ApplicationModel, app_id)
            assert app is not None
            vacancy = VacancyRepository(session).upsert(
                VacancyData("202", "Другой диалог", "https://hh.ru/vacancy/202")
            )
            wrong = ApplicationRepository(session).create_apply_intent(
                account_id,
                vacancy.id,
                app.resume_id,
            )
            repo = CommunicationRepository(session)
            canonical, _ = repo.save_synced_message(
                application_id=app_id,
                hh_id="501",
                direction=MessageDirection.OUTGOING,
                body=LETTER,
                occurred_at=datetime.now(UTC),
            )
            duplicate = repo.save_incoming(
                application_id=wrong.id,
                hh_id="501",
                body=LETTER,
                received_at=datetime.now(UTC),
            )
            incident = IncidentService(session).report(
                code="HH_MESSAGE_CONTENT_CONFLICT",
                severity=IncidentSeverity.WARNING,
                scope_type="recruiter_message",
                scope_id=duplicate.id,
                message="Чужой диалог",
            )
            invitation = repo.save_invitation(
                application_id=wrong.id,
                hh_id="message:501",
                title="Задание от работодателя",
                details=LETTER,
                interview_at=None,
                booking_url=None,
                updated_at=datetime.now(UTC),
            )
            sync = HhSynchronizationService(session)
            with pytest.raises(CommunicationStateError):
                sync.resolve_duplicate_chat_import(
                    account_id=account_id,
                    message_id=duplicate.id,
                    verified_message=HhChatMessageData(
                        "101",
                        "501",
                        MessageDirection.OUTGOING,
                        "Другой текст",
                        direction_inferred=False,
                    ),
                )
            assert incident.state is IncidentState.OPEN
            observed = HhChatMessageData(
                "101",
                "501",
                MessageDirection.OUTGOING,
                LETTER,
                direction_inferred=False,
            )
            before_count = UiWorkspaceService(session).dashboard(account_id).new_messages
            NotificationService(session).collect(account_id)

            assert (
                sync.resolve_duplicate_chat_import(
                    account_id=account_id,
                    message_id=duplicate.id,
                    verified_message=observed,
                )
                == canonical.id
            )

            row = session.get(RecruiterMessageModel, duplicate.id)
            assert row is not None
            assert (row.body, row.hh_id, row.application_id, row.created_at) == (
                duplicate.body,
                duplicate.hh_id,
                duplicate.application_id,
                duplicate.created_at,
            )
            assert row.state is RecruiterMessageState.CANCELLED
            assert row.superseded_by_id == canonical.id
            invitation_row = session.get(InvitationModel, invitation.id)
            assert invitation_row is not None and invitation_row.state is InvitationState.CLOSED
            assert (
                UiWorkspaceService(session).dashboard(account_id).new_messages == before_count - 1
            )
            NotificationService(session).collect(account_id)
            assert not tuple(
                session.scalars(
                    select(NotificationModel).where(
                        NotificationModel.deduplication_key.like(
                            f"message:{duplicate.id}:NEW_MESSAGE:%"
                        ),
                        NotificationModel.state == DeliveryState.PENDING,
                    )
                )
            )
            periods, _ = application_statistics(
                session,
                {wrong.id: datetime.now(UTC)},
                now=datetime.now(UTC),
                timezone_name="UTC",
                viewed=set(),
                rejected=set(),
                invited=set(),
                interviewed=set(),
                scheduled=set(),
                recently_checked=set(),
            )
            assert all(period.replies == 0 for period in periods)
            persisted_incident = session.get(IncidentModel, incident.id)
            assert persisted_incident is not None
            assert persisted_incident.state is IncidentState.RESOLVED
            BackgroundProcessService(session, account_id).set_enabled("replies", True)
            assert ReplyWorker(settings, account_id=account_id)._claim(session) is None
            assert not next(
                c
                for c in UiCommunicationService(session).get(account_id).conversations
                if c.application_id == wrong.id
            ).needs_reply
            assert (
                sync.resolve_duplicate_chat_import(
                    account_id=account_id,
                    message_id=duplicate.id,
                    verified_message=observed,
                )
                == canonical.id
            )
    finally:
        db.close()


@pytest.mark.parametrize("unknown", [False, True])
def test_cancelling_obsolete_generated_failure_resolves_only_retryable_alert(
    settings: Settings, unknown: bool
) -> None:
    db = create_database(settings)
    try:
        with db.sessions.begin() as session:
            account_id, app_id = saved_letter(session)
            repo = CommunicationRepository(session)
            source = repo.save_incoming(
                application_id=app_id, hh_id="old", body="Вопрос", received_at=datetime.now(UTC)
            )
            draft = CommunicationService(session, RecordingMessageSender()).create_outgoing_draft(
                application_id=app_id,
                body="Автоматический ответ",
                reply_to_message_id=source.id,
            )
            row = session.get(RecruiterMessageModel, draft.id)
            parent = session.get(RecruiterMessageModel, source.id)
            assert row is not None and parent is not None
            row.state = (
                RecruiterMessageState.UNKNOWN_RESULT if unknown else RecruiterMessageState.FAILED
            )
            parent.state = RecruiterMessageState.CANCELLED
            code = "RECRUITER_MESSAGE_SEND_UNKNOWN" if unknown else "RECRUITER_MESSAGE_SEND_FAILED"
            incident = IncidentService(session).report(
                code=code,
                severity=IncidentSeverity.ERROR,
                scope_type="recruiter_message",
                scope_id=row.id,
                message="Ошибка отправки",
            )
            NotificationService(session).collect(account_id)
            assert repo.cancel_unneeded_generated_replies(account_id=account_id) == (
                0 if unknown else 1
            )
            assert incident.state is (IncidentState.OPEN if unknown else IncidentState.RESOLVED)
            if not unknown:
                assert repo.claim_due_notification(datetime.now(UTC)) is None
            NotificationService(session).collect(account_id)
            if not unknown:
                assert not tuple(
                    session.scalars(
                        select(NotificationModel).where(
                            NotificationModel.incident_id == incident.id,
                            NotificationModel.state == DeliveryState.PENDING,
                        )
                    )
                )
            assert row.state is (
                RecruiterMessageState.UNKNOWN_RESULT if unknown else RecruiterMessageState.CANCELLED
            )
    finally:
        db.close()
