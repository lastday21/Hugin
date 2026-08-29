# ruff: noqa: RUF001

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationModel,
    InvitationModel,
    NotificationModel,
    RecruiterMessageActionModel,
    RecruiterMessageModel,
)
from hugin.domain.communications import (
    CommunicationNotFoundError,
    CommunicationStateError,
    InvitationRecord,
    MessageSendOutcome,
    NotificationRecord,
    RecruiterMessageActionRecord,
    RecruiterMessageRecord,
    StaleMessageDraftError,
)
from hugin.domain.content import (
    DeliveryState,
    InvitationState,
    MessageDirection,
    NotificationChannel,
    RecruiterActionKind,
    RecruiterActionSource,
    RecruiterActionState,
    RecruiterMessageState,
)
from hugin.domain.directions import ConfigPayload
from hugin.domain.time import as_utc

_NON_RETRYABLE_NOTIFICATION_ERRORS = frozenset(
    {
        "EMAIL_NOT_CONFIGURED",
        "HISTORICAL_EVENT_SUPPRESSED",
        "NOTIFICATION_SERVICE_NOT_CONFIGURED",
        "TELEGRAM_NOT_CONFIGURED",
    }
)
_UNKNOWN_OUTGOING_RECONCILIATION_WINDOW = timedelta(minutes=30)


def _optional_utc(value: datetime | None) -> datetime | None:
    return as_utc(value) if value is not None else None


def _message_record(model: RecruiterMessageModel) -> RecruiterMessageRecord:
    return RecruiterMessageRecord(
        id=model.id,
        application_id=model.application_id,
        hh_id=model.hh_id,
        direction=model.direction,
        body=model.body,
        state=model.state,
        content_hash=model.content_hash,
        content_version=model.version,
        read_at=_optional_utc(model.read_at),
        confirmed_at=_optional_utc(model.confirmed_at),
        sent_at=_optional_utc(model.sent_at),
        received_at=_optional_utc(model.received_at),
        auto_send_approved=model.auto_send_approved,
        reply_template_key=model.reply_template_key,
        created_at=as_utc(model.created_at),
    )


def _message_action_record(model: RecruiterMessageActionModel) -> RecruiterMessageActionRecord:
    return RecruiterMessageActionRecord(
        message_id=model.message_id,
        kind=model.kind,
        state=model.state,
        source=model.source,
        reason_code=model.reason_code,
        reason=model.reason,
        due_at=_optional_utc(model.due_at),
        resolved_at=_optional_utc(model.resolved_at),
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _invitation_record(model: InvitationModel) -> InvitationRecord:
    return InvitationRecord(
        id=model.id,
        application_id=model.application_id,
        hh_id=model.hh_id,
        title=model.title,
        details=model.details,
        interview_at=_optional_utc(model.interview_at),
        booking_url=model.booking_url,
        state=model.state,
        seen_at=_optional_utc(model.seen_at),
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _notification_record(model: NotificationModel) -> NotificationRecord:
    return NotificationRecord(
        id=model.id,
        application_id=model.application_id,
        incident_id=model.incident_id,
        deduplication_key=model.deduplication_key,
        event_type=model.event_type,
        channel=model.channel,
        state=model.state,
        payload=cast(ConfigPayload, dict(model.payload)),
        scheduled_at=as_utc(model.scheduled_at),
        sent_at=_optional_utc(model.sent_at),
        error_code=model.error_code,
        created_at=as_utc(model.created_at),
    )


class CommunicationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_messages_for_account(
        self,
        account_id: int,
    ) -> tuple[RecruiterMessageRecord, ...]:
        models = self._session.scalars(
            select(RecruiterMessageModel)
            .join(
                ApplicationModel,
                ApplicationModel.id == RecruiterMessageModel.application_id,
            )
            .where(ApplicationModel.account_id == account_id)
            .order_by(RecruiterMessageModel.created_at.desc(), RecruiterMessageModel.id.desc())
        )
        return tuple(_message_record(model) for model in models)

    def save_incoming(
        self,
        *,
        application_id: int,
        hh_id: str,
        body: str,
        received_at: datetime,
    ) -> RecruiterMessageRecord:
        self._require_application(application_id)
        statement = (
            insert(RecruiterMessageModel)
            .values(
                application_id=application_id,
                hh_id=hh_id,
                direction=MessageDirection.INCOMING,
                body=body,
                state=RecruiterMessageState.RECEIVED,
                version=1,
                received_at=as_utc(received_at),
            )
            .on_conflict_do_nothing(
                constraint="uq_recruiter_messages_application_hh_id",
            )
            .returning(RecruiterMessageModel.id)
        )
        message_id = self._session.scalar(statement)
        if message_id is None:
            model = self._session.scalar(
                select(RecruiterMessageModel).where(
                    RecruiterMessageModel.application_id == application_id,
                    RecruiterMessageModel.hh_id == hh_id,
                )
            )
            if model is None:
                raise RuntimeError("Не удалось получить сохранённое входящее сообщение")
            if model.direction is not MessageDirection.INCOMING or model.body != body:
                raise CommunicationStateError(
                    "Идентификатор входящего сообщения уже связан с другим содержимым"
                )
            return _message_record(model)
        model = self._session.get(RecruiterMessageModel, message_id)
        if model is None:
            raise RuntimeError("Не удалось получить новое входящее сообщение")
        return _message_record(model)

    def save_synced_message(
        self,
        *,
        application_id: int,
        hh_id: str,
        direction: MessageDirection,
        body: str,
        occurred_at: datetime,
    ) -> tuple[RecruiterMessageRecord, bool]:
        self._require_application(application_id)
        selected_at = as_utc(occurred_at)
        existing = self._session.scalar(
            select(RecruiterMessageModel)
            .where(
                RecruiterMessageModel.application_id == application_id,
                RecruiterMessageModel.hh_id == hh_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.direction is not direction or existing.body != body:
                raise CommunicationStateError(
                    "Идентификатор сообщения hh.ru уже связан с другим содержимым"
                )
            return _message_record(existing), False

        incoming = direction is MessageDirection.INCOMING
        if not incoming:
            reconciled = self._reconcile_unknown_outgoing(
                application_id=application_id,
                hh_id=hh_id,
                body=body,
                sent_at=selected_at,
            )
            if reconciled is not None:
                return reconciled, False
        statement = (
            insert(RecruiterMessageModel)
            .values(
                application_id=application_id,
                hh_id=hh_id,
                direction=direction,
                body=body,
                state=(RecruiterMessageState.RECEIVED if incoming else RecruiterMessageState.SENT),
                version=1,
                received_at=selected_at if incoming else None,
                sent_at=selected_at if not incoming else None,
            )
            .on_conflict_do_nothing(
                constraint="uq_recruiter_messages_application_hh_id",
            )
            .returning(RecruiterMessageModel.id)
        )
        message_id = self._session.scalar(statement)
        created = message_id is not None
        if message_id is None:
            model = self._session.scalar(
                select(RecruiterMessageModel).where(
                    RecruiterMessageModel.application_id == application_id,
                    RecruiterMessageModel.hh_id == hh_id,
                )
            )
            if model is None:
                raise RuntimeError("Не удалось получить синхронизированное сообщение")
            if model.direction is not direction or model.body != body:
                raise CommunicationStateError(
                    "Идентификатор сообщения hh.ru уже связан с другим содержимым"
                )
        else:
            model = self._session.get(RecruiterMessageModel, message_id)
            if model is None:
                raise RuntimeError("Не удалось получить новое сообщение hh.ru")
        return _message_record(model), created

    def mark_incoming_read(
        self,
        account_id: int,
        message_id: int,
        read_at: datetime,
    ) -> RecruiterMessageRecord:
        model = self._message_model(account_id, message_id, for_update=True)
        if model.direction is not MessageDirection.INCOMING:
            raise CommunicationStateError("Прочитанным можно отметить только входящее сообщение")
        if model.read_at is None:
            model.read_at = as_utc(read_at)
            self._session.flush()
        return _message_record(model)

    def list_message_actions_for_account(
        self,
        account_id: int,
    ) -> tuple[RecruiterMessageActionRecord, ...]:
        models = self._session.scalars(
            select(RecruiterMessageActionModel)
            .join(
                RecruiterMessageModel,
                RecruiterMessageModel.id == RecruiterMessageActionModel.message_id,
            )
            .join(
                ApplicationModel,
                ApplicationModel.id == RecruiterMessageModel.application_id,
            )
            .where(ApplicationModel.account_id == account_id)
            .order_by(
                RecruiterMessageActionModel.message_id,
                RecruiterMessageActionModel.kind,
            )
        )
        return tuple(_message_action_record(model) for model in models)

    def record_message_action(
        self,
        *,
        account_id: int,
        message_id: int,
        kind: RecruiterActionKind,
        state: RecruiterActionState,
        source: RecruiterActionSource,
        reason_code: str,
        reason: str,
        due_at: datetime | None,
        changed_at: datetime,
        preserve_resolved: bool,
    ) -> RecruiterMessageActionRecord:
        message = self._message_model(account_id, message_id, for_update=True)
        if message.direction is not MessageDirection.INCOMING:
            raise CommunicationStateError(
                "Состояние действия можно сохранить только для входящего сообщения"
            )
        model = self._session.get(
            RecruiterMessageActionModel,
            (message_id, kind),
            with_for_update=True,
        )
        resolved_states = {
            RecruiterActionState.COMPLETED,
            RecruiterActionState.DISMISSED,
            RecruiterActionState.NOT_REQUIRED,
        }
        if model is not None and preserve_resolved and model.state in resolved_states:
            return _message_action_record(model)

        resolved_at = None if state is RecruiterActionState.REQUIRED else as_utc(changed_at)
        if model is None:
            model = RecruiterMessageActionModel(
                message_id=message_id,
                kind=kind,
                state=state,
                source=source,
                reason_code=reason_code,
                reason=reason,
                due_at=_optional_utc(due_at),
                resolved_at=resolved_at,
                created_at=as_utc(changed_at),
                updated_at=as_utc(changed_at),
            )
            self._session.add(model)
        else:
            model.state = state
            model.source = source
            model.reason_code = reason_code
            model.reason = reason
            if due_at is not None or state is RecruiterActionState.REQUIRED:
                model.due_at = _optional_utc(due_at)
            model.resolved_at = resolved_at
            model.updated_at = as_utc(changed_at)
        self._session.flush()
        return _message_action_record(model)

    def dismiss_expired_message_actions(
        self,
        *,
        account_id: int,
        changed_at: datetime,
    ) -> int:
        selected_at = as_utc(changed_at)
        models = tuple(
            self._session.scalars(
                select(RecruiterMessageActionModel)
                .join(
                    RecruiterMessageModel,
                    RecruiterMessageModel.id == RecruiterMessageActionModel.message_id,
                )
                .join(
                    ApplicationModel,
                    ApplicationModel.id == RecruiterMessageModel.application_id,
                )
                .where(
                    ApplicationModel.account_id == account_id,
                    RecruiterMessageActionModel.state == RecruiterActionState.REQUIRED,
                    RecruiterMessageActionModel.due_at.is_not(None),
                    RecruiterMessageActionModel.due_at <= selected_at,
                )
                .with_for_update()
            )
        )
        for model in models:
            due_at = as_utc(model.due_at) if model.due_at is not None else selected_at
            model.state = RecruiterActionState.DISMISSED
            model.source = RecruiterActionSource.SYSTEM
            model.reason_code = "ACTION_EXPIRED"
            model.reason = f"Срок выполнения действия истёк {due_at.isoformat()}."
            model.resolved_at = selected_at
            model.updated_at = selected_at
        if models:
            self._session.flush()
        return len(models)

    def complete_reply_action_for_sent_outgoing(
        self,
        *,
        account_id: int,
        message_id: int,
        completed_at: datetime,
    ) -> RecruiterMessageActionRecord | None:
        outgoing = self._message_model(account_id, message_id, for_update=True)
        if (
            outgoing.direction is not MessageDirection.OUTGOING
            or outgoing.state is not RecruiterMessageState.SENT
        ):
            return None
        incoming_id = self._session.scalar(
            select(RecruiterMessageModel.id)
            .where(
                RecruiterMessageModel.application_id == outgoing.application_id,
                RecruiterMessageModel.direction == MessageDirection.INCOMING,
                RecruiterMessageModel.id < outgoing.id,
            )
            .order_by(RecruiterMessageModel.id.desc())
            .limit(1)
        )
        if incoming_id is None:
            return None
        action = self._session.get(
            RecruiterMessageActionModel,
            (incoming_id, RecruiterActionKind.REPLY),
            with_for_update=True,
        )
        if action is None or action.state is not RecruiterActionState.REQUIRED:
            return _message_action_record(action) if action is not None else None
        selected_at = as_utc(completed_at)
        action.state = RecruiterActionState.COMPLETED
        action.source = RecruiterActionSource.SYSTEM
        action.reason_code = "REPLY_SENT"
        action.reason = "Ответ на входящее сообщение подтверждённо отправлен в hh.ru."
        action.resolved_at = selected_at
        action.updated_at = selected_at
        self._session.flush()
        return _message_action_record(action)

    def create_outgoing_draft(
        self,
        *,
        application_id: int,
        body: str,
        content_hash: str,
        auto_send_approved: bool = False,
        reply_template_key: str | None = None,
    ) -> RecruiterMessageRecord:
        self._require_application(application_id)
        model = RecruiterMessageModel(
            application_id=application_id,
            direction=MessageDirection.OUTGOING,
            body=body,
            content_hash=content_hash,
            version=1,
            state=RecruiterMessageState.REVIEW_REQUIRED,
            auto_send_approved=auto_send_approved,
            reply_template_key=reply_template_key,
        )
        self._session.add(model)
        self._session.flush()
        return _message_record(model)

    def _reconcile_unknown_outgoing(
        self,
        *,
        application_id: int,
        hh_id: str,
        body: str,
        sent_at: datetime,
    ) -> RecruiterMessageRecord | None:
        selected_at = as_utc(sent_at)
        unknown = tuple(
            self._session.scalars(
                select(RecruiterMessageModel)
                .where(
                    RecruiterMessageModel.application_id == application_id,
                    RecruiterMessageModel.direction == MessageDirection.OUTGOING,
                    RecruiterMessageModel.state == RecruiterMessageState.UNKNOWN_RESULT,
                    RecruiterMessageModel.hh_id.is_(None),
                    RecruiterMessageModel.confirmed_at.is_not(None),
                    RecruiterMessageModel.confirmed_at <= selected_at,
                    RecruiterMessageModel.confirmed_at
                    >= selected_at - _UNKNOWN_OUTGOING_RECONCILIATION_WINDOW,
                )
                .with_for_update()
            )
        )
        candidates = tuple(model for model in unknown if model.body == body)
        if not candidates:
            stripped = body.strip()
            candidates = tuple(model for model in unknown if model.body.strip() == stripped)
        if len(candidates) != 1:
            return None
        model = candidates[0]
        model.hh_id = hh_id
        model.state = RecruiterMessageState.SENT
        model.sent_at = selected_at
        self._session.flush()
        return _message_record(model)

    def edit_outgoing_draft(
        self,
        *,
        account_id: int,
        message_id: int,
        body: str,
        content_hash: str,
    ) -> RecruiterMessageRecord:
        model = self._message_model(account_id, message_id, for_update=True)
        self._require_outgoing(model)
        if model.state in {
            RecruiterMessageState.SENT,
            RecruiterMessageState.UNKNOWN_RESULT,
        }:
            raise CommunicationStateError(
                "Отправленную версию или сообщение с неизвестным результатом нельзя менять"
            )
        if model.body == body and model.content_hash == content_hash:
            return _message_record(model)
        model.body = body
        model.content_hash = content_hash
        model.version += 1
        model.state = RecruiterMessageState.REVIEW_REQUIRED
        model.confirmed_at = None
        model.sent_at = None
        self._session.flush()
        return _message_record(model)

    def approve_outgoing_for_automatic_send(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
        approval_key: str,
    ) -> RecruiterMessageRecord:
        model = self._message_model(account_id, message_id, for_update=True)
        self._require_outgoing(model)
        self._require_exact_version(model, content_version, content_hash)
        if model.state not in {
            RecruiterMessageState.DRAFT,
            RecruiterMessageState.REVIEW_REQUIRED,
            RecruiterMessageState.FAILED,
        }:
            raise CommunicationStateError(
                "Автоматически подтвердить можно только неотправленный ответ"
            )
        model.auto_send_approved = True
        model.reply_template_key = approval_key
        self._session.flush()
        return _message_record(model)

    def confirm_outgoing_draft(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
        confirmed_at: datetime,
    ) -> RecruiterMessageRecord:
        model = self._message_model(account_id, message_id, for_update=True)
        self._require_outgoing(model)
        self._require_exact_version(model, content_version, content_hash)
        if model.state in {
            RecruiterMessageState.CONFIRMED,
            RecruiterMessageState.SENT,
            RecruiterMessageState.UNKNOWN_RESULT,
        }:
            return _message_record(model)
        if model.state not in {
            RecruiterMessageState.DRAFT,
            RecruiterMessageState.REVIEW_REQUIRED,
            RecruiterMessageState.FAILED,
        }:
            raise CommunicationStateError("Черновик нельзя подтвердить в текущем состоянии")
        model.state = RecruiterMessageState.CONFIRMED
        model.confirmed_at = as_utc(confirmed_at)
        self._session.flush()
        return _message_record(model)

    def record_send_outcome(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
        outcome: MessageSendOutcome,
        external_id: str | None,
        finished_at: datetime,
    ) -> RecruiterMessageRecord:
        model = self._message_model(account_id, message_id, for_update=True)
        self._require_outgoing(model)
        self._require_exact_version(model, content_version, content_hash)
        if model.state is not RecruiterMessageState.CONFIRMED:
            return _message_record(model)
        states = {
            MessageSendOutcome.SENT: RecruiterMessageState.SENT,
            MessageSendOutcome.FAILED: RecruiterMessageState.FAILED,
            MessageSendOutcome.UNKNOWN_RESULT: RecruiterMessageState.UNKNOWN_RESULT,
        }
        model.state = states[outcome]
        if outcome is MessageSendOutcome.SENT:
            model.sent_at = as_utc(finished_at)
            if external_id:
                model.hh_id = external_id
        self._session.flush()
        return _message_record(model)

    def get_message(
        self,
        account_id: int,
        message_id: int,
    ) -> RecruiterMessageRecord:
        return _message_record(self._message_model(account_id, message_id))

    def lock_message_for_send(
        self,
        account_id: int,
        message_id: int,
    ) -> RecruiterMessageRecord:
        return _message_record(
            self._message_model(
                account_id,
                message_id,
                for_update=True,
            )
        )

    def list_invitations_for_account(
        self,
        account_id: int,
    ) -> tuple[InvitationRecord, ...]:
        models = self._session.scalars(
            select(InvitationModel)
            .join(
                ApplicationModel,
                ApplicationModel.id == InvitationModel.application_id,
            )
            .where(ApplicationModel.account_id == account_id)
            .order_by(InvitationModel.created_at.desc(), InvitationModel.id.desc())
        )
        return tuple(_invitation_record(model) for model in models)

    def save_invitation(
        self,
        *,
        application_id: int,
        hh_id: str,
        title: str,
        details: str | None,
        interview_at: datetime | None,
        booking_url: str | None,
        updated_at: datetime,
    ) -> InvitationRecord:
        self._require_application(application_id)
        selected_at = as_utc(updated_at)
        statement = (
            insert(InvitationModel)
            .values(
                application_id=application_id,
                hh_id=hh_id,
                title=title,
                details=details,
                interview_at=as_utc(interview_at) if interview_at is not None else None,
                booking_url=booking_url,
                state=InvitationState.RECEIVED,
                updated_at=selected_at,
            )
            .on_conflict_do_update(
                constraint="uq_invitations_application_hh_id",
                set_={
                    "title": title,
                    "details": details,
                    "interview_at": (as_utc(interview_at) if interview_at is not None else None),
                    "booking_url": booking_url,
                    "updated_at": selected_at,
                },
            )
            .returning(InvitationModel.id)
        )
        invitation_id = self._session.scalar(statement)
        if invitation_id is None:
            raise RuntimeError("Не удалось сохранить приглашение")
        model = self._session.get(InvitationModel, invitation_id)
        if model is None:
            raise RuntimeError("Не удалось получить сохранённое приглашение")
        self._session.refresh(model)
        return _invitation_record(model)

    def has_open_message_invitation(
        self,
        application_id: int,
        title: str,
    ) -> bool:
        self._require_application(application_id)
        invitation_id = self._session.scalar(
            select(InvitationModel.id)
            .where(
                InvitationModel.application_id == application_id,
                InvitationModel.hh_id.like("message:%"),
                InvitationModel.title == title,
                InvitationModel.state != InvitationState.CLOSED,
            )
            .limit(1)
        )
        return invitation_id is not None

    def close_status_invitations(
        self,
        application_id: int,
        updated_at: datetime,
    ) -> int:
        self._require_application(application_id)
        closed_ids = self._session.scalars(
            update(InvitationModel)
            .where(
                InvitationModel.application_id == application_id,
                InvitationModel.hh_id.like("status:%"),
                InvitationModel.state != InvitationState.CLOSED,
            )
            .values(
                state=InvitationState.CLOSED,
                updated_at=as_utc(updated_at),
            )
            .returning(InvitationModel.id)
        )
        return len(tuple(closed_ids))

    def mark_invitation_seen(
        self,
        account_id: int,
        invitation_id: int,
        seen_at: datetime,
    ) -> InvitationRecord:
        model = self._invitation_model(account_id, invitation_id, for_update=True)
        if model.seen_at is None:
            model.seen_at = as_utc(seen_at)
            self._session.flush()
        return _invitation_record(model)

    def enqueue_notification(
        self,
        *,
        deduplication_key: str,
        event_type: str,
        channel: NotificationChannel,
        payload: ConfigPayload,
        scheduled_at: datetime,
        application_id: int | None = None,
        incident_id: int | None = None,
    ) -> NotificationRecord:
        statement = (
            insert(NotificationModel)
            .values(
                application_id=application_id,
                incident_id=incident_id,
                deduplication_key=deduplication_key,
                event_type=event_type,
                channel=channel,
                state=DeliveryState.PENDING,
                payload=dict(payload),
                scheduled_at=as_utc(scheduled_at),
            )
            .on_conflict_do_nothing(
                constraint="uq_notifications_deduplication_key",
            )
            .returning(NotificationModel.id)
        )
        notification_id = self._session.scalar(statement)
        if notification_id is None:
            model = self._session.scalar(
                select(NotificationModel).where(
                    NotificationModel.deduplication_key == deduplication_key
                )
            )
            if model is None:
                raise RuntimeError("Не удалось получить сохранённое уведомление")
            return _notification_record(model)
        model = self._session.get(NotificationModel, notification_id)
        if model is None:
            raise RuntimeError("Не удалось получить новое уведомление")
        return _notification_record(model)

    def claim_due_notification(self, now: datetime) -> NotificationRecord | None:
        model = self._session.scalar(
            select(NotificationModel)
            .where(
                NotificationModel.state.in_(
                    {
                        DeliveryState.PENDING,
                        DeliveryState.FAILED,
                    }
                ),
                NotificationModel.scheduled_at <= as_utc(now),
                or_(
                    NotificationModel.error_code.is_(None),
                    NotificationModel.error_code.not_in(_NON_RETRYABLE_NOTIFICATION_ERRORS),
                ),
            )
            .order_by(NotificationModel.scheduled_at, NotificationModel.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return _notification_record(model) if model is not None else None

    def mark_notification_sent(
        self,
        notification_id: int,
        sent_at: datetime,
    ) -> NotificationRecord:
        model = self._session.get(NotificationModel, notification_id)
        if model is None:
            raise CommunicationNotFoundError("Уведомление не найдено")
        if model.state is DeliveryState.SENT:
            return _notification_record(model)
        model.state = DeliveryState.SENT
        model.sent_at = as_utc(sent_at)
        model.error_code = None
        self._session.flush()
        return _notification_record(model)

    def mark_notification_failed(
        self,
        notification_id: int,
        *,
        error_code: str,
        retry_at: datetime,
    ) -> NotificationRecord:
        model = self._session.get(NotificationModel, notification_id)
        if model is None:
            raise CommunicationNotFoundError("Уведомление не найдено")
        if model.state is DeliveryState.SENT:
            return _notification_record(model)
        selected_code = error_code.strip().upper()[:64]
        model.state = DeliveryState.FAILED
        model.error_code = selected_code or "DELIVERY_FAILED"
        model.scheduled_at = as_utc(retry_at)
        self._session.flush()
        return _notification_record(model)

    def defer_notifications(
        self,
        channel: NotificationChannel,
        until: datetime,
        *,
        excluding_id: int | None = None,
    ) -> int:
        conditions = [
            NotificationModel.channel == channel,
            NotificationModel.state.in_({DeliveryState.PENDING, DeliveryState.FAILED}),
            NotificationModel.scheduled_at < as_utc(until),
        ]
        if excluding_id is not None:
            conditions.append(NotificationModel.id != excluding_id)
        deferred_ids = self._session.scalars(
            update(NotificationModel)
            .where(*conditions)
            .values(scheduled_at=as_utc(until))
            .returning(NotificationModel.id)
        )
        return len(tuple(deferred_ids))

    def _message_model(
        self,
        account_id: int,
        message_id: int,
        *,
        for_update: bool = False,
    ) -> RecruiterMessageModel:
        statement = (
            select(RecruiterMessageModel)
            .join(
                ApplicationModel,
                ApplicationModel.id == RecruiterMessageModel.application_id,
            )
            .where(
                RecruiterMessageModel.id == message_id,
                ApplicationModel.account_id == account_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        model = self._session.scalar(statement)
        if model is None:
            raise CommunicationNotFoundError("Сообщение не найдено")
        return model

    def _invitation_model(
        self,
        account_id: int,
        invitation_id: int,
        *,
        for_update: bool = False,
    ) -> InvitationModel:
        statement = (
            select(InvitationModel)
            .join(
                ApplicationModel,
                ApplicationModel.id == InvitationModel.application_id,
            )
            .where(
                InvitationModel.id == invitation_id,
                ApplicationModel.account_id == account_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        model = self._session.scalar(statement)
        if model is None:
            raise CommunicationNotFoundError("Приглашение не найдено")
        return model

    def _require_application(self, application_id: int) -> None:
        if self._session.get(ApplicationModel, application_id) is None:
            raise CommunicationNotFoundError("Отклик не найден")

    @staticmethod
    def _require_outgoing(model: RecruiterMessageModel) -> None:
        if model.direction is not MessageDirection.OUTGOING:
            raise CommunicationStateError("Действие доступно только для исходящего черновика")

    @staticmethod
    def _require_exact_version(
        model: RecruiterMessageModel,
        content_version: int,
        content_hash: str,
    ) -> None:
        if model.version != content_version or model.content_hash != content_hash:
            raise StaleMessageDraftError(
                "Черновик изменился. Проверьте актуальную версию перед подтверждением"
            )
